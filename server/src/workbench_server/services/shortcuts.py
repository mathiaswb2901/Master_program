r"""shortcuts.md: parser (pure) + the service that loads, merges and watches it.

The file is written by a human as a note and read by a forgiving parser: an H2
heading names a shortcut, optional ``key: value`` lines configure it, and a
fenced code block is its body. A malformed entry is skipped and reported in
``ShortcutsState.problems`` — parsing never raises and never loses the rest of
the file.

Security (non-negotiable): nothing here executes anything. A ``shell`` entry is
text the UI *types* into the active terminal without a trailing newline; a
``prompt`` entry is text it puts in the chat draft. The user presses Enter. A
shell body is therefore required to be printable text only: in a live PTY the
control bytes are key events, not characters — ``\n`` is Enter, ``\x0f`` is
accept-line in readline and PSReadLine, ``\x03`` aborts, ``\x1b`` opens an
editing escape sequence — so any of them would act the moment the body landed.

The third kind, ``layout``, is the one entry that *acts* rather than inserts —
and it stays inside the same doctrine because its whole vocabulary is the name of
an arrangement the user themselves saved. It moves panels and nothing else: no
text reaches a shell or an agent, no file is touched, and a name nobody saved is
a no-op with a toast. Its body is constrained to a single printable line no
longer than a layout name, so it cannot smuggle a payload for a future consumer.

The fourth kind, ``command``, is what lets a file bind *anything the registry
knows* without a parser change for each capability (ROADMAP M5 item 4, from
tmux's ".tmux.conf rebinds every command"). Its body is a registered command id
— a short single-line token this parser bounds and keeps printable, exactly as
``layout`` bounds a name. The parser cannot know the registry, so it does not
decide *which* command is meant or whether it is safe: the UI resolves the id and
refuses one that is unknown or that declared itself unbindable from a file (a
command reaching a filesystem path or re-pointing the jail — ``workspace.open``
is the named one, ROADMAP item 5). What lands here is only ever a request, never
execution, which is the same bar every other kind clears.
"""

import asyncio
import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from watchfiles import awatch

from workbench_server.models.files import FileChangedEvent
from workbench_server.models.layouts import MAX_NAME_CHARS as MAX_LAYOUT_NAME_CHARS
from workbench_server.models.shortcuts import (
    MAX_BODY_CHARS,
    MAX_COMMAND_ID_CHARS,
    MAX_DETAIL_CHARS,
    MAX_FILE_BYTES,
    MAX_NAME_CHARS,
    ShortcutEntry,
    ShortcutKind,
    ShortcutProblem,
    ShortcutsChangedEvent,
    ShortcutSource,
    ShortcutsState,
)
from workbench_server.services.event_bus import EventBus

log = structlog.get_logger()

WORKSPACE_SHORTCUTS_PATH = ".workbench/shortcuts.md"
WORKSPACE_LABEL = ".workbench/shortcuts.md"
GLOBAL_LABEL = "~/.workbench/shortcuts.md"


def global_shortcuts_path() -> Path:
    """The user-global file. Not a setting: one well-known path, like the file itself."""
    return Path.home() / ".workbench" / "shortcuts.md"


# ---- chords ----------------------------------------------------------------

_MODIFIERS = {"ctrl": "ctrl", "cmd": "ctrl", "meta": "ctrl", "alt": "alt", "shift": "shift"}


@dataclass(frozen=True)
class _Chord:
    ctrl: bool
    alt: bool
    shift: bool
    key: str

    def canonical(self) -> str:
        return f"{int(self.ctrl)}{int(self.alt)}{int(self.shift)}:{self.key}"


def parse_chord(text: str) -> _Chord | None:
    """Mirror of ``ui/src/keys.ts`` ``parseChord``. None = no real key in it."""
    ctrl = alt = shift = False
    key = ""
    for raw in text.split("+"):
        part = raw.strip().lower()
        if part == "":
            continue
        modifier = _MODIFIERS.get(part)
        if modifier == "ctrl":
            ctrl = True
        elif modifier == "alt":
            alt = True
        elif modifier == "shift":
            shift = True
        else:
            key = part
    return _Chord(ctrl, alt, shift, key) if key else None


def _reserved() -> frozenset[str]:
    """Chords the built-in registry owns (DESIGN.md §6.8).

    Advisory duplication: ``ui/src/commands.ts`` is authoritative and drops a
    colliding chord at merge time regardless. Listing them here is what turns a
    collision into a *message the user sees* instead of a binding that silently
    never fires.

    Only the Alt-carrying ones can actually be *requested* by a file (see
    ``_resolve_chord``); the Ctrl ones are listed so asking for one gets the
    precise "built-in shortcut" message instead of the generic refusal.
    """
    chords = [
        "Ctrl+P",
        "Ctrl+K",
        "Ctrl+Shift+P",
        "Ctrl+S",
        "Ctrl+PageDown",
        "Alt+PageDown",
        "Ctrl+PageUp",
        "Alt+PageUp",
        "Alt+W",
        "Ctrl+F4",
        "Alt+T",
        "Alt+M",
        *[f"Ctrl+{n}" for n in range(1, 5)],
        *[f"Alt+{n}" for n in range(1, 10)],
    ]
    return frozenset(chord.canonical() for text in chords if (chord := parse_chord(text)))


RESERVED_CHORDS = _reserved()


# ---- parser ----------------------------------------------------------------

_HEADING_RE = re.compile(r"^##\s+(?P<name>\S.*?)\s*$")
_META_RE = re.compile(r"^(?:[-*]\s+)?(?P<key>[A-Za-z][A-Za-z0-9_-]*)\s*:\s*(?P<value>.*)$")
_FENCE_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})\s*(?P<info>\S*)")

_META_KEYS = frozenset({"type", "keys", "detail"})
_KINDS: frozenset[str] = frozenset({"shell", "prompt", "layout", "command"})

# C0 controls + DEL. Printable text is what a shell body may be, and nothing else.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _control_reason(char: str) -> str:
    """Message for a control byte, naming the common one so it reads as advice."""
    if char == "\n":
        return "shell body must be a single line"
    return f"shell body must be printable text (found {char!r})"


@dataclass
class _Raw:
    """One H2 block, before validation."""

    name: str
    meta: dict[str, str] = field(default_factory=dict)
    body: list[str] | None = None  # None = the block had no fenced block at all
    info: str = ""
    closed: bool = False


@dataclass
class ParsedFile:
    """What one shortcuts file contributed."""

    label: str
    entries: list[ShortcutEntry] = field(default_factory=list)
    problems: list[ShortcutProblem] = field(default_factory=list)


def _split_blocks(text: str) -> tuple[list[_Raw], bool]:
    """Cut the file into H2 blocks. Deliberately dumb: never raises.

    Every fence is tracked, not just the one an entry is collecting: markdown
    inside a fenced block is *example text*, so a ``##`` line in the preamble's
    ```` ```markdown ```` sample (docs/shortcuts.md ships exactly that shape) is
    prose, not a heading that registers a live command.

    Returns the blocks and whether the file ended inside a fence no entry owned
    — everything after such a fence was swallowed, and the caller says so.
    """
    blocks: list[_Raw] = []
    current: _Raw | None = None
    fence_char = ""
    fence_len = 0
    collecting = False  # the open fence is the current block's body
    for line in text.splitlines():
        if fence_len:
            stripped = line.strip()
            if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
                fence_len = 0
                if collecting and current is not None:
                    current.closed = True
                collecting = False
                continue
            if collecting and current is not None and current.body is not None:
                current.body.append(line)
            continue
        fence = _FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group("fence")
            fence_char, fence_len = marker[0], len(marker)
            # Only an entry that has no body yet claims the fence; any other one
            # is tracked purely so its contents stay inert.
            collecting = current is not None and current.body is None
            if collecting and current is not None:
                current.info = fence.group("info").lower()
                current.body = []
            continue
        heading = _HEADING_RE.match(line)
        if heading is not None:
            current = _Raw(name=heading.group("name"))
            blocks.append(current)
            continue
        if current is None or current.body is not None:
            continue  # preamble, or prose after the body — both ignored
        meta = _META_RE.match(line.strip())
        if meta is not None:
            key = meta.group("key").lower()
            if key in _META_KEYS and key not in current.meta:
                current.meta[key] = meta.group("value").strip()
    return blocks, bool(fence_len) and not collecting


def _resolve_kind(raw: _Raw) -> str:
    """Explicit ``type:`` wins; a fence tagged with a kind selects it (```prompt,
    ```layout); otherwise shell — so a ```powershell body stays what it looks like."""
    declared = raw.meta.get("type")
    if declared is not None:
        return declared.lower()
    return raw.info if raw.info in _KINDS else "shell"


def _resolve_chord(
    raw_keys: str, name: str, label: str, taken: set[str], problems: list[ShortcutProblem]
) -> str | None:
    """Validated chord text, or None with a problem appended. The entry survives
    a bad chord — it is still reachable from the QuickBar."""

    def reject(reason: str) -> None:
        problems.append(ShortcutProblem(file=label, message=f"{name}: {reason}"))

    chord = parse_chord(raw_keys)
    if chord is None:
        reject(f"unusable chord {raw_keys!r} — binding ignored")
        return None
    if not chord.alt:
        # Alt is the only modifier a file may ask for, for two reasons that point
        # the same way. Reach: keys.ts never intercepts a plain key (typing must
        # always arrive at the surface) and inside Monaco/xterm only Alt and
        # Ctrl+Shift chords are taken, so Alt is the one that works everywhere.
        # Safety: outside those two surfaces `isIntercepted` hands us *any*
        # Ctrl chord, so a file asking for Ctrl+V or Ctrl+A would take paste or
        # select-all away from the chat box and every other input in the app.
        reject(f"chord {raw_keys!r} must include Alt to be bindable — binding ignored")
        return None
    canonical = chord.canonical()
    if canonical in RESERVED_CHORDS:
        reject(f"chord {raw_keys!r} is a built-in shortcut — binding ignored")
        return None
    if canonical in taken:
        reject(f"chord {raw_keys!r} is already bound — binding ignored")
        return None
    taken.add(canonical)
    return raw_keys


def parse_shortcuts(text: str, source: ShortcutSource, label: str) -> ParsedFile:
    """Parse one shortcuts file. Pure, total: bad entries become problems."""
    parsed = ParsedFile(label=label)
    seen: set[str] = set()
    taken_chords: set[str] = set()

    def reject(name: str, reason: str) -> None:
        parsed.problems.append(ShortcutProblem(file=label, message=f"{name}: {reason}"))

    blocks, dangling_fence = _split_blocks(text)
    if dangling_fence:
        parsed.problems.append(
            ShortcutProblem(
                file=label, message="unterminated code fence — everything below it was ignored"
            )
        )
    for raw in blocks:
        name = raw.name.strip()
        if len(name) > MAX_NAME_CHARS:
            reject(name[:MAX_NAME_CHARS], f"name longer than {MAX_NAME_CHARS} characters — skipped")
            continue
        if name.casefold() in seen:
            reject(name, "duplicate name in this file — later entry skipped")
            continue
        seen.add(name.casefold())

        if raw.body is None:
            reject(name, "no fenced code block — the block is the shortcut body")
            continue
        if not raw.closed:
            reject(name, "unterminated code fence — skipped")
            continue
        body = "\n".join(raw.body).strip("\n").rstrip()
        if body == "":
            reject(name, "empty body — skipped")
            continue
        if len(body) > MAX_BODY_CHARS:
            reject(name, f"body longer than {MAX_BODY_CHARS} characters — skipped")
            continue

        declared = _resolve_kind(raw)
        if declared not in _KINDS:
            reject(
                name,
                f"unknown type {declared!r} (expected shell, prompt, layout or command) — skipped",
            )
            continue
        kind: ShortcutKind = (
            "prompt"
            if declared == "prompt"
            else "layout"
            if declared == "layout"
            else "command"
            if declared == "command"
            else "shell"
        )
        if kind == "shell" and (control := _CONTROL_RE.search(body)) is not None:
            # Control bytes are key events in a live PTY, not characters: \n and
            # \r are Enter, \x0f is accept-line (readline, and PSReadLine in
            # Emacs mode), \x03 aborts, \x1b starts an editing escape sequence
            # that ConPTY turns back into virtual keys. Any of them would act on
            # insertion — the one thing a shortcut may never do.
            reject(name, f"{_control_reason(control.group())} — skipped")
            continue
        if kind == "layout":
            # The body is a layout *name*, nothing else. Bounded and single-line
            # so this kind can never carry a payload some later consumer reads.
            if _CONTROL_RE.search(body) is not None:
                reject(name, "layout body must be a single line naming a layout — skipped")
                continue
            if len(body) > MAX_LAYOUT_NAME_CHARS:
                reject(
                    name,
                    f"layout name longer than {MAX_LAYOUT_NAME_CHARS} characters — skipped",
                )
                continue
        if kind == "command":
            # The body is a registered command *id*, nothing else — a short,
            # single-line, printable token. Whether that id is known and whether
            # it is safe to bind from a file are the UI's to decide (only it holds
            # the registry); the parser guarantees the shape and nothing more.
            if _CONTROL_RE.search(body) is not None:
                reject(name, "command body must be a single line naming a command — skipped")
                continue
            if len(body) > MAX_COMMAND_ID_CHARS:
                reject(
                    name,
                    f"command id longer than {MAX_COMMAND_ID_CHARS} characters — skipped",
                )
                continue

        raw_keys = raw.meta.get("keys", "")
        keys = (
            _resolve_chord(raw_keys, name, label, taken_chords, parsed.problems)
            if raw_keys
            else None
        )
        detail = raw.meta.get("detail") or None
        parsed.entries.append(
            ShortcutEntry(
                name=name,
                kind=kind,
                body=body,
                keys=keys,
                detail=detail[:MAX_DETAIL_CHARS] if detail is not None else None,
                source=source,
            )
        )
    return parsed


def merge_shortcuts(primary: ParsedFile, secondary: ParsedFile) -> ShortcutsState:
    """Merge two parsed files. ``primary`` (the workspace) wins: a name it
    already defines shadows the other file's entry silently — that is the
    documented rule, not an error — and a chord it already took is dropped from
    the secondary entry with a problem."""
    entries: list[ShortcutEntry] = []
    problems: list[ShortcutProblem] = [*primary.problems, *secondary.problems]
    names: set[str] = set()
    chords: set[str] = set()
    for parsed in (primary, secondary):
        for entry in parsed.entries:
            if entry.name.casefold() in names:
                continue
            names.add(entry.name.casefold())
            chord = parse_chord(entry.keys) if entry.keys is not None else None
            if chord is not None:
                if chord.canonical() in chords:
                    problems.append(
                        ShortcutProblem(
                            file=parsed.label,
                            message=f"{entry.name}: chord {entry.keys!r} is already bound "
                            "— binding ignored",
                        )
                    )
                    entry = entry.model_copy(update={"keys": None})
                else:
                    chords.add(chord.canonical())
            entries.append(entry)
    return ShortcutsState(entries=entries, problems=problems)


# ---- service ---------------------------------------------------------------


class ShortcutsService:
    """Loads both files, keeps the merged state, and reloads it live.

    The workspace file rides the existing watcher (its `FileChangedEvent` on the
    bus is the trigger). The global file lives outside the workspace, so it gets
    its own small watch on the same `watchfiles` mechanism.
    """

    def __init__(
        self, workspace_root: Path, bus: EventBus, global_path: Path | None = None
    ) -> None:
        self._workspace_file = workspace_root.resolve() / Path(WORKSPACE_SHORTCUTS_PATH)
        self._global_file = global_path or global_shortcuts_path()
        self._bus = bus
        self._state = ShortcutsState(entries=[], problems=[])
        self._stop = asyncio.Event()
        self._bus_task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None

    def state(self) -> ShortcutsState:
        return self._state

    def set_workspace_root(self, root: Path) -> None:
        """Read the new workspace's file instead, and publish if it says
        something different.

        Neither watch is restarted. The workspace half rides the shared bus and
        matches on the *relative* path, which the re-rooted watcher now reports
        against the new root; the global half watches a path in the user's home
        that a workspace switch does not move.
        """
        self._workspace_file = root.resolve() / Path(WORKSPACE_SHORTCUTS_PATH)
        self._reload_and_publish()

    def reload(self) -> bool:
        """Re-read both files. Returns True when the merged state changed."""
        state = merge_shortcuts(
            self._read(self._workspace_file, "workspace", WORKSPACE_LABEL),
            self._read(self._global_file, "global", GLOBAL_LABEL),
        )
        changed = state != self._state
        self._state = state
        return changed

    def start(self) -> None:
        self.reload()
        log.info(
            "shortcuts.loaded",
            entries=len(self._state.entries),
            problems=len(self._state.problems),
        )
        self._bus_task = asyncio.create_task(self._watch_workspace(), name="shortcuts-workspace")
        self._watch_task = asyncio.create_task(self._watch_global(), name="shortcuts-global")

    async def stop(self) -> None:
        """Stop both watches, awaiting each — like ``Watcher.stop``.

        The global watch exits its own ``async for`` on the stop event; cancelling
        it instead would raise inside ``awatch``, leave the async generator
        unfinalized and keep the watchfiles thread alive until GC. The bus
        subscriber only ever waits on its queue, so cancel is its only way out.
        """
        self._stop.set()
        if self._bus_task is not None:
            self._bus_task.cancel()
        for task in (self._bus_task, self._watch_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._bus_task = self._watch_task = None

    def _read(self, path: Path, source: ShortcutSource, label: str) -> ParsedFile:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return ParsedFile(
                    label=label,
                    problems=[
                        ShortcutProblem(
                            file=label, message=f"larger than {MAX_FILE_BYTES // 1024} KB — ignored"
                        )
                    ],
                )
            text = path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ParsedFile(label=label)  # both files are optional
        except OSError as err:
            return ParsedFile(
                label=label,
                problems=[
                    ShortcutProblem(file=label, message=f"unreadable: {err.strerror or err}")
                ],
            )
        return parse_shortcuts(text, source, label)

    def _reload_and_publish(self) -> None:
        if not self.reload():
            return
        self._bus.publish(
            ShortcutsChangedEvent(
                entry_count=len(self._state.entries),
                problem_count=len(self._state.problems),
            )
        )
        log.info(
            "shortcuts.reloaded",
            entries=len(self._state.entries),
            problems=len(self._state.problems),
        )

    async def _watch_workspace(self) -> None:
        """The workspace watcher already reports this file — subscribe, don't re-watch."""
        queue = self._bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if isinstance(event, FileChangedEvent) and event.path == WORKSPACE_SHORTCUTS_PATH:
                    self._reload_and_publish()
        finally:
            self._bus.unsubscribe(queue)

    async def _watch_global(self) -> None:
        directory = self._global_file.parent
        if not directory.is_dir():
            # Nothing to watch yet; a global file created later is picked up on
            # the next server start. Keeping a poll alive for it is not worth it.
            log.debug("shortcuts.global_absent", path=str(self._global_file))
            return
        name = self._global_file.name
        async for changes in awatch(
            directory, stop_event=self._stop, debounce=200, step=50, recursive=False
        ):
            if any(Path(raw).name == name for _, raw in changes):
                self._reload_and_publish()
