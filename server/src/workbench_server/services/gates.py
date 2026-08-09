"""The toolchain gate — M6 staged review, PR 1's ``ValidationCheck``.

M6 proved *numbers*: a workbook reconciles with the code, unit- and DST-aware.
This check generalises the proof from "workbooks proven" to **work proven** — it
runs the project's own gate commands (``ruff``, ``mypy``, ``pytest``,
``npm-test``) and turns each into one ``gate`` :class:`EvidenceItem` whose
``payload_ref`` names the captured log. Nothing downstream of a check changes:
risk derivation, the gallery, the bus event, the replay and the one mandatory
human approval are the #82 frame's and are untouched.

**Evidence, never authority.** This module produces evidence and stops. There is
no call in it that can record an approval, and ``test_gates.py`` asserts that
rather than leaving it to review.

Four decisions carry the safety story, and each is a line of code rather than a
comment:

**1. A caller names a gate; the server owns the argv.** :data:`GATE_CATALOG` is
built in. A :class:`~workbench_server.models.gates.GateSpec` selects from it by
id, and every process starts through ``asyncio.create_subprocess_exec`` —
*never* ``shell=True``, so nothing in a request is ever interpreted by a shell.
The operator, not the workspace, chooses which gates and how long they may take
(``WORKBENCH_GATES``, ``WORKBENCH_GATE_TIMEOUT_S``). A per-workspace
``.workbench/gates.json`` is **refused and said out loud** (see
:func:`workspace_config_refusal`): a config file inside a folder would mean that
*opening* a project is enough to run its commands, which is the escalation the
PreToolUse broker exists to stop, arriving by a side door.

**2. It runs in the session's own slot, and nowhere else.** The pool
(``services/worktrees.py``) hands a Mission Control worker a borrowed checkout;
:class:`SlotLocator` is the seam through which this module asks *which* one,
without importing the orchestrator. **No lease is taken** — the session holds it,
and acquiring a second slot would validate a different tree than the one the
agent wrote. A session with no slot gets a **refusal**, never a fallback to the
live workspace root: running ``pytest`` there would write caches into the folder
the user is editing and judge a tree that includes their unsaved changes.

**3. The tree is fingerprinted before and after.** ``git rev-parse HEAD`` plus
the ``--porcelain`` count, on both sides. If either moved, the whole run is
``skipped`` — never a pass or a fail attributed to a tree that no longer exists.
This is ``_verify_reset``'s posture in ``services/worktrees.py`` applied one
level up: two git processes with a gap between them means you re-read rather than
hope. Silent green is the enemy this milestone exists to kill.

**4. The log is bounded while the pipes drain.** :class:`_BoundedCapture` keeps a
head buffer and a ring tail as output arrives, so a gate that prints 500 MB costs
:data:`~workbench_server.models.gates.MAX_GATE_LOG_BYTES` of memory rather than
500 MB — not a post-hoc truncation of an unbounded buffer.

**Fake-first.** :class:`GateRunner` has two implementations, the Office-host
split exactly: :class:`SubprocessGateRunner` (real) and :class:`FakeGateRunner`
(scripted exit codes and canned output), selected by ``WORKBENCH_GATE_FAKE=1``.
CI proves the whole flow with no real toolchain run.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import ValidationError

from workbench_server.models.gates import (
    MAX_GATE_LOG_BYTES,
    GateCommand,
    GateLog,
    GateRunReport,
    GateSpec,
    SlotRef,
    head_bytes,
)
from workbench_server.models.validation import EvidenceItem, EvidenceTruncation
from workbench_server.services.validation import ValidationContext
from workbench_server.services.worktrees import GIT_TIMEOUT_S, GitError, GitRunner, run_git

log = structlog.get_logger()

#: The built-in catalog. Server-owned data: an operator picks *which* of these
#: run and how long they may take, and nobody — REST caller or agent — supplies
#: an argv. Adding a fifth shape is one row here and one line in its test.
#:
#: The commands are this project's own gates (``CLAUDE.md``), run through ``uv``
#: so a slot with its own ``.venv`` is used rather than whatever interpreter the
#: server happens to be on.
GATE_CATALOG: tuple[GateCommand, ...] = (
    GateCommand(
        id="ruff",
        argv=("uv", "run", "ruff", "check", "."),
        label="ruff check .",
        timeout_s=180.0,
    ),
    GateCommand(
        id="mypy",
        argv=("uv", "run", "mypy"),
        label="mypy --strict",
        timeout_s=600.0,
    ),
    GateCommand(
        id="pytest",
        argv=("uv", "run", "pytest", "-q"),
        label="pytest -q",
        timeout_s=1_800.0,
    ),
    GateCommand(
        id="npm-test",
        argv=("npm", "--prefix", "ui", "run", "test"),
        label="npm run test (ui)",
        timeout_s=900.0,
    ),
)

#: What an empty :attr:`GateSpec.gates` means when the operator named nothing.
DEFAULT_GATE_IDS: tuple[str, ...] = tuple(command.id for command in GATE_CATALOG)

#: The per-workspace config file this feature deliberately does **not** read.
#: Named as a constant so the refusal and its test quote the same string.
WORKSPACE_GATES_FILE = Path(".workbench") / "gates.json"

#: Bytes pulled off the merged pipe per read. Small enough that a gate which
#: prints continuously is bounded promptly, large enough not to be a syscall
#: per line.
_READ_CHUNK = 8_192

#: Env the settings knob names, quoted in every refusal that it would fix.
SETTING_GATES = "WORKBENCH_GATES"
SETTING_GATE_TIMEOUT = "WORKBENCH_GATE_TIMEOUT_S"

#: The refusal a session working outside the pool gets. One sentence, from one
#: place, because the agent tool and the evidence line must not word it twice.
NO_SLOT_DETAIL = (
    "this session holds no worktree slot; gates run in the checkout the session "
    "writes in, never in the live workspace root (that would judge your unsaved "
    "changes and write caches into the folder you are editing). Start the work as "
    "a Mission Control worker — an orchestrator's worker borrows a pool slot — and "
    "run the gates against that session."
)


# ---- the seams ---------------------------------------------------------------


class SlotLocator(Protocol):
    """Which checkout is this session writing in? ``None`` when it holds none.

    Implemented by ``OrchestratorService`` (which owns the roster) and injected,
    so this module never imports the orchestrator — the ``CommandInvoker``
    pattern, one more time.
    """

    def slot_of(self, session_id: str) -> SlotRef | None: ...


class GateRunner(Protocol):
    """Run one gate in one directory and hand back its bounded log.

    Two implementations, the Office-host split: the real one spawns a process,
    the fake one reads a script. ``window`` is the captured byte budget.
    """

    async def run(self, command: GateCommand, cwd: Path, window: int) -> GateLog: ...


# ---- bounded capture ---------------------------------------------------------


class _BoundedCapture:
    """A head buffer and a ring tail, filled as the pipe drains.

    The alternative — read everything, truncate at the end — is a gate that
    prints 500 MB costing 500 MB of the server's memory before anyone decides it
    was too much. This costs the window, whatever the process does.
    """

    def __init__(self, window: int) -> None:
        self._head_cap = head_bytes(window)
        self._tail_cap = max(0, window - self._head_cap)
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._head_cap - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if not chunk or self._tail_cap <= 0:
            return
        self._tail += chunk
        overflow = len(self._tail) - self._tail_cap
        if overflow > 0:
            del self._tail[:overflow]

    def render(self) -> tuple[str, EvidenceTruncation | None]:
        """The captured text, and what was withheld (AXI shape 1).

        The seam between head and tail is **marked in the text itself**, not only
        in ``truncated``: a reader scrolling a log must not have to know that two
        adjacent lines were never adjacent. That marker is the only reason
        ``len(text)`` can exceed the window, and it is a fixed ~60 bytes.
        """
        head = self._head.decode("utf-8", errors="replace")
        tail = self._tail.decode("utf-8", errors="replace")
        shown = len(self._head) + len(self._tail)
        withheld = self.total - shown
        if withheld <= 0:
            return head + tail, None
        marker = f"\n… {withheld} bytes withheld …\n"
        return (
            head + marker + tail,
            EvidenceTruncation(
                shown=shown,
                total=self.total,
                detail=(
                    f"showing {shown} of {self.total} bytes of output (head + tail); "
                    f"{withheld} withheld — pass log_bytes up to {MAX_GATE_LOG_BYTES} "
                    "to widen the window"
                ),
            ),
        )


# ---- the runners -------------------------------------------------------------


class SubprocessGateRunner:
    """The real one: one process per gate, bounded, with nothing to prompt at.

    ``create_subprocess_exec``, never ``shell=True`` — the argv is the catalog's
    and is passed through as separate arguments, so there is no string for a
    shell to reinterpret. ``stdin`` is ``DEVNULL`` and ``GIT_TERMINAL_PROMPT=0``
    for the same reason ``run_git`` sets it: a tool that stops to ask something
    inside a request nobody can cancel is a hang, not a failure.
    """

    async def run(self, command: GateCommand, cwd: Path, window: int) -> GateLog:
        started = time.monotonic()
        capture = _BoundedCapture(window)
        env = dict(
            os.environ,
            GIT_TERMINAL_PROMPT="0",
            NO_COLOR="1",
            PYTHONUNBUFFERED="1",
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *command.argv,
                cwd=cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                # Interleaved on purpose: a gate's diagnosis is split across both
                # streams (ruff writes findings to stdout and its summary to
                # stderr), and two separately-bounded buffers would drop the half
                # that mattered.
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
        except OSError as err:
            capture.feed(f"could not start {command.argv[0]!r}: {err}".encode())
            return self._log(command, None, started, capture)
        try:
            await asyncio.wait_for(self._drain(proc, capture), command.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            await proc.wait()
            capture.feed(f"\n[killed: no exit after {command.timeout_s:.0f}s]".encode())
            return self._log(command, None, started, capture)
        return self._log(command, proc.returncode, started, capture)

    @staticmethod
    async def _drain(proc: asyncio.subprocess.Process, capture: _BoundedCapture) -> None:
        stream = proc.stdout
        if stream is not None:
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    break
                capture.feed(chunk)
        await proc.wait()

    @staticmethod
    def _log(
        command: GateCommand, code: int | None, started: float, capture: _BoundedCapture
    ) -> GateLog:
        text, truncated = capture.render()
        return GateLog(
            gate=command.id,
            argv=list(command.argv),
            exit_code=code,
            duration_ms=round((time.monotonic() - started) * 1000),
            text=text,
            truncated=truncated,
        )


class FakeGateRunner:
    """Scripted gates: exit codes and canned output, no process anywhere.

    The ``WORKBENCH_OFFICE_FAKE`` posture — off by default, loudly logged when
    on, and it drives the *whole* flow so CI proves the mechanism rather than the
    toolchain. The shipped script fails ``pytest`` and passes the rest, because a
    fake that only ever passes would leave the failing half — the half whose
    captured log is the entire point — unproven in the browser.
    """

    #: gate id → (exit code, canned output). Anything unlisted passes silently.
    DEFAULT_SCRIPT: Mapping[str, tuple[int, str]] = {
        "ruff": (0, "All checks passed!\n"),
        "mypy": (0, "Success: no issues found in 214 source files\n"),
        # The ``path:line`` is deliberate: it is what ``run_gates`` reads to end
        # its result with where to look next (AXI shape 3), so a fake without one
        # would leave that branch unproven everywhere CI can reach.
        "pytest": (
            1,
            "server/tests/test_dispatch.py::test_gate_closure FAILED\n"
            "server/tests/test_dispatch.py:118: assert 17 == 18  # bid window closed early\n"
            "1 failed, 118 passed in 12.40s\n",
        ),
        "npm-test": (0, "Test Files  31 passed (31)\n"),
    }

    def __init__(
        self,
        script: Mapping[str, tuple[int, str]] | None = None,
        *,
        delay_s: float = 0.0,
    ) -> None:
        self._script = dict(self.DEFAULT_SCRIPT if script is None else script)
        self._delay_s = delay_s

    async def run(self, command: GateCommand, cwd: Path, window: int) -> GateLog:
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        code, text = self._script.get(command.id, (0, f"{command.id}: ok (fake)\n"))
        capture = _BoundedCapture(window)
        capture.feed(text.encode())
        rendered, truncated = capture.render()
        return GateLog(
            gate=command.id,
            argv=list(command.argv),
            exit_code=code,
            duration_ms=1,
            text=rendered,
            truncated=truncated,
        )


def build_runner(fake: bool) -> GateRunner:
    """The one construction point, so ``main.py`` states the choice once."""
    if fake:
        log.warning(
            "gates.fake_mode_enabled",
            detail="WORKBENCH_GATE_FAKE is set: gate results are scripted, nothing is run",
        )
        return FakeGateRunner()
    return SubprocessGateRunner()


# ---- the catalog, as the operator configured it -------------------------------


def build_catalog(timeout_s: float | None = None) -> dict[str, GateCommand]:
    """The catalog, with the operator's ceiling applied if they named one.

    ``WORKBENCH_GATE_TIMEOUT_S`` replaces *every* gate's timeout rather than
    adding a per-gate knob: one number a user can reason about beats four they
    have to keep consistent, and the per-gate defaults above are already sized
    for the shapes they run.
    """
    catalog: dict[str, GateCommand] = {}
    for command in GATE_CATALOG:
        catalog[command.id] = (
            command if timeout_s is None else command.model_copy(update={"timeout_s": timeout_s})
        )
    return catalog


def configured_gate_ids(raw: str) -> tuple[str, ...]:
    """Parse ``WORKBENCH_GATES``. Empty (or all blanks) means the default set.

    Comma-separated rather than JSON: this is a knob a person types into a shell,
    and ``WORKBENCH_GATES=ruff,mypy`` is what they will type.
    """
    named = tuple(part.strip() for part in raw.split(",") if part.strip())
    return named or DEFAULT_GATE_IDS


def workspace_config_refusal(*roots: Path) -> EvidenceItem | None:
    """Refuse a per-workspace ``.workbench/gates.json``, **out loud**.

    Deferred with a reason rather than a shrug (``docs/plan/staged-review.md``):
    opening a folder must never be sufficient to run that folder's commands. If
    it ever ships it needs an explicit one-time trust prompt, which is a feature.
    Until then the file is not read, and a workspace carrying one is *told* so —
    a config that is silently ignored is how an operator comes to believe a gate
    ran that never did.
    """
    for root in roots:
        candidate = root / WORKSPACE_GATES_FILE
        if not candidate.is_file():
            continue
        log.warning("gates.workspace_config_refused", path=str(candidate))
        return EvidenceItem(
            kind="gate",
            label="workspace gate configuration",
            outcome="skipped",
            detail=(
                f"{WORKSPACE_GATES_FILE.as_posix()} was found and deliberately not read — "
                "opening a folder must never be enough to run that folder's commands. "
                f"Configure gates on the server instead: {SETTING_GATES} selects them and "
                f"{SETTING_GATE_TIMEOUT} bounds them."
            ),
        )
    return None


# ---- the fingerprint ----------------------------------------------------------


@dataclass(frozen=True)
class _Fingerprint:
    """What a checkout looked like at one instant: its commit and its dirt."""

    head: str
    dirty: int


def _now_utc() -> datetime:
    return datetime.now(UTC)


# ---- the check ----------------------------------------------------------------


class ToolchainGateCheck:
    """The registered gate. ``id`` is the handle a ``ValidationSpec`` names."""

    id = "gates"

    def __init__(
        self,
        locator: SlotLocator,
        runner: GateRunner,
        *,
        catalog: Mapping[str, GateCommand] | None = None,
        default_gates: Sequence[str] | None = None,
        git: GitRunner = run_git,
    ) -> None:
        self._locator = locator
        self._runner = runner
        self._catalog = dict(catalog) if catalog is not None else build_catalog()
        self._default_gates = tuple(default_gates or DEFAULT_GATE_IDS)
        self._git = git

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        try:
            spec = GateSpec.model_validate(ctx.params)
        except ValidationError as exc:
            return [_refusal(f"invalid gate spec: {exc.error_count()} error(s)")]

        session_id = _session_of(ctx)
        if session_id is None:
            return [
                _refusal(
                    "gates run against a session's own checkout, so the subject must be a "
                    f"session output — this one is {ctx.subject.kind!r}."
                )
            ]
        slot = self._locator.slot_of(session_id)
        if slot is None:
            return [_refusal(NO_SLOT_DETAIL)]
        path = Path(slot.path)
        # ``to_thread`` for both, and not as ceremony: ``is_dir`` and ``is_file``
        # are ``stat`` calls, and a blocking syscall inside a coroutine is exactly
        # what ``ASYNC240`` exists to stop — a slow network path would stall the
        # loop this request shares with every socket the window holds open.
        if not await asyncio.to_thread(path.is_dir):
            return [
                _refusal(
                    f"the checkout this session was working in is gone ({slot.path}) — "
                    "its slot was released or pruned. Re-run once the session has one again."
                )
            ]

        config_refusal = await asyncio.to_thread(workspace_config_refusal, path, ctx.root)
        preface = [config_refusal] if config_refusal is not None else []

        before = await self._fingerprint(path)
        if before is None:
            return [
                *preface,
                _refusal(
                    f"git could not read {slot.slot or slot.path} — a checkout whose state "
                    "cannot be vouched for is not one a gate result may be attributed to."
                ),
            ]

        selected, unknown = self._select(spec)
        started_at = _now_utc()
        started = time.monotonic()
        window = spec.window()
        logs = [await self._runner.run(command, path, window) for command in selected]
        duration_ms = round((time.monotonic() - started) * 1000)

        after = await self._fingerprint(path)
        if after is None or after != before:
            # Never a pass and never a fail: the tree these logs describe is not
            # the tree that is there now, and a verdict attributed to it would be
            # exactly the silent green this milestone exists to refuse.
            log.warning(
                "gates.tree_moved",
                slot=slot.slot,
                before=before.head[:12],
                after=None if after is None else after.head[:12],
            )
            return [*preface, _moved(before, after, len(logs))]

        report = GateRunReport(
            path=slot.path,
            slot=slot.slot,
            head=before.head,
            gates=logs,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        log.info(
            "gates.completed",
            slot=slot.slot,
            head=before.head[:12],
            gates=[entry.gate for entry in report.gates],
            duration_ms=duration_ms,
        )
        return [
            *preface,
            *unknown,
            *(self._evidence(entry, ctx) for entry in report.gates),
        ]

    # ---- pieces -------------------------------------------------------------

    def _select(self, spec: GateSpec) -> tuple[list[GateCommand], list[EvidenceItem]]:
        """Resolve ids to commands. An unknown id is a ``fail`` line naming what
        is available — never a silent skip (the frame's unregistered-check
        precedent, which exists for exactly this mistake)."""
        wanted = spec.gates or list(self._default_gates)
        commands: list[GateCommand] = []
        unknown: list[EvidenceItem] = []
        available = ", ".join(sorted(self._catalog))
        for gate_id in wanted:
            command = self._catalog.get(gate_id)
            if command is None:
                unknown.append(
                    EvidenceItem(
                        kind="gate",
                        label=gate_id,
                        outcome="fail",
                        detail=(
                            f"no gate {gate_id!r} is configured on this server — "
                            f"available: {available}. The catalog is server-owned; a "
                            f"caller names a gate, never a command."
                        ),
                    )
                )
                continue
            commands.append(command)
        return commands, unknown

    def _evidence(self, entry: GateLog, ctx: ValidationContext) -> EvidenceItem:
        """One gate, one line — not one grouped line.

        The opposite of ``ReconciliationCheck``, on purpose: reconciliation
        groups because it has forty comparisons and one question, whereas four
        gates are four independent questions whose answers a reader wants side by
        side. Per-gate outcomes also let ``derive_risk`` do the right thing — a
        failing ``pytest`` is ``high`` even when ``ruff`` is clean.
        """
        command = self._catalog.get(entry.gate)
        label = command.label if command is not None else entry.gate
        seconds = entry.duration_ms / 1000
        ref = ctx.store_payload("gate", entry)
        if entry.exit_code is None:
            # No exit code covers both "killed at the ceiling" and "never
            # started". The log's head carries which — that is what the head half
            # of the window is for — so the line names the log rather than
            # guessing between them.
            return EvidenceItem(
                kind="gate",
                label=label,
                outcome="fail",
                detail=(
                    f"{label}: no exit code after {seconds:.1f}s — it timed out or could "
                    f"not start; open the log. Raise {SETTING_GATE_TIMEOUT} if this gate "
                    "needs longer."
                ),
                payload_ref=ref,
            )
        passed = command is None or entry.exit_code in command.pass_codes
        if passed:
            return EvidenceItem(
                kind="gate",
                label=label,
                outcome="pass",
                detail=f"{label}: exit {entry.exit_code} in {seconds:.1f}s.",
                payload_ref=ref,
            )
        captured = (
            entry.truncated.total if entry.truncated is not None else len(entry.text.encode())
        )
        return EvidenceItem(
            kind="gate",
            label=label,
            outcome="fail",
            detail=(
                f"{label}: exit {entry.exit_code} in {seconds:.1f}s, "
                f"{captured} bytes of output captured — open the log."
            ),
            payload_ref=ref,
        )

    async def _fingerprint(self, path: Path) -> _Fingerprint | None:
        """``HEAD`` plus the dirty-file count, or ``None`` when git would not say.

        ``None`` is not "clean" and callers must not read it as one: a checkout
        whose state cannot be vouched for is one no verdict may be attributed to
        (``services/worktrees.py``'s ``_dirty_count`` rule, one level up).
        """
        try:
            head = await self._git(path, ("rev-parse", "HEAD"), GIT_TIMEOUT_S)
            status = await self._git(
                path, ("--no-optional-locks", "status", "--porcelain"), GIT_TIMEOUT_S
            )
        except GitError as err:
            log.warning("gates.fingerprint_failed", path=str(path), detail=str(err))
            return None
        if not head.ok or not head.out or not status.ok:
            log.warning("gates.fingerprint_failed", path=str(path), detail=head.first_error_line())
            return None
        dirty = len([line for line in status.out.splitlines() if line.strip()])
        return _Fingerprint(head=head.out.splitlines()[0].strip(), dirty=dirty)


def _session_of(ctx: ValidationContext) -> str | None:
    """The session whose checkout is being judged, or ``None``.

    Only a ``session_output`` subject names one. A ``file`` or ``objective``
    subject is a perfectly good thing to reconcile and not a thing that has a
    checkout, so it is refused rather than resolved to something plausible.
    """
    return ctx.subject.ref if ctx.subject.kind == "session_output" else None


def _refusal(detail: str) -> EvidenceItem:
    """One ``skipped`` line. Refusing is the feature — a fallback would be the
    bug — and a refusal that says why and how to fix it is not an absence."""
    return EvidenceItem(kind="gate", label="toolchain gates", outcome="skipped", detail=detail)


def _moved(before: _Fingerprint, after: _Fingerprint | None, ran: int) -> EvidenceItem:
    """The tree moved under the gate. ``skipped``, never a pass."""
    if after is None:
        moved = "git could not re-read the checkout afterwards"
    elif after.head != before.head:
        moved = f"HEAD moved from {before.head[:12]} to {after.head[:12]}"
    else:
        moved = f"the working tree changed ({before.dirty} → {after.dirty} file(s))"
    return _refusal(
        f"the tree moved under the gate while it ran — {moved}. "
        f"{ran} gate(s) ran, and their results are discarded rather than attributed "
        "to a tree that no longer exists. Re-run when the session is idle."
    )
