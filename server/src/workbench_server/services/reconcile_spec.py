"""Spec-from-code — **ambient CI for workbooks**. Save the workbook; seconds
later the chip flips. Nobody fires a check.

``ReconciliationCheck`` takes its ``expected`` values as data, and that decision
stands: ``models/reconciliation.py`` is explicit that executing user code out of
a JSON body is what the never-execute doctrine forbids. What it left open is that
somebody has to *produce* the data every time, which is why the gate was fired by
hand and therefore fired rarely. This module closes that loop the way the repo
already closes this class of problem: the **file** names the callable, the
**server** owns the argv, and a **one-time content-hash approval** is what turns a
file in a folder into something that may run.

The mechanism, and where it came from
-------------------------------------
``services/gates.py`` (#115) is reused for its *mechanism* and deliberately not
for its *catalog*. Four things are taken and the fifth is left:

* :class:`~workbench_server.services.gates._BoundedCapture` — head buffer plus
  ring tail, bounded *while the pipe drains*. A spec whose function prints a
  500 MB dataframe costs the window, not the memory.
* the :class:`~workbench_server.services.gates.GateRunner` *shape* — a Protocol
  with a real implementation and a fake, selected by an env knob
  (:data:`~workbench_server.models.reconcile_spec.SETTING_FAKE`), so CI proves
  the whole flow with no user code executed anywhere.
* ``create_subprocess_exec``, never ``shell=True``; stdin carries the payload and
  nothing is interpreted by a shell.
* the per-run timeout and the "no exit code" branch, which covers both "killed at
  the ceiling" and "never started" and names the setting that raises it.

What is **not** taken is the catalog. ``models/gates.py`` says it plainly —
"there is no field anywhere in this module through which a JSON body can reach an
argv, a cwd or a path" — so a caller-named callable can never be a ``GateCommand``.
:data:`SPEC_ENTRY_ARGV` is fixed and server-owned, and the only variable input is
the spec document, delivered on **stdin**.

The posture difference, and what pays for it
--------------------------------------------
``ToolchainGateCheck`` refuses a session with no pool slot rather than falling
back to the live workspace root, because running ``pytest`` there would judge the
user's unsaved changes and write caches into the folder they are editing. This
module **runs in the workspace root on purpose** — the workbook and the analyst's
own code are there, and there is nothing else to point at. That is a real
widening, and it is paid for by the approval below: specifically by that approval
being keyed to the **code**, not merely to the spec that names it, which is what
makes it the "explicit one-time trust prompt, which is a feature" that
:func:`~workbench_server.services.gates.workspace_config_refusal` names as the
price of admission for reading a config file out of a folder. This is that
feature, built. It does **not** unlock ``.workbench/gates.json``, which stays
refused, out loud, in the same words.

The approval, in one sentence the panel uses verbatim: *this spec, running
exactly this code, on this machine, until either changes.*

Why the composite digest, stated as the defect it prevents
-----------------------------------------------------------
``blake2b(spec bytes)`` alone would be a hole, and a bad one. The bytes in
``.workbench/reconcile/dispatch.toml`` are not the code that runs; the code lives
in ``se3/reporting.py``, which the spec only *names*. Approve once, then edit
``annual_revenue``'s body, and the spec file — and therefore the approval — is
untouched, while the watcher keeps firing the gate on every workbook save,
running whatever that function now contains, unattended, forever. So the digest
folds in, in declared order: the spec's own bytes; the bytes of every module a
``callable`` resolves to; and the workspace-local import closure the previous run
*actually used* (a fact from ``sys.modules``, not a static guess at an import
graph that can be conditional or built with ``importlib``).

It is **re-verified at every run**, never only at approval time. A mismatch does
not silently re-run and does not silently skip: it is a refusal naming *which*
file changed and offering re-approval. A gate that runs code nobody re-approved
is the silent green again, wearing a different hat.

The honest consequence of the closure, both halves: the **first** approved run of
a spec is covered only to the entry points it named, and from the second run on,
changing *any* workspace file that actually participated revokes it.

The loop
--------
Once approved, a ``FileChangedEvent`` for the spec's **workbook** re-runs the
gate, debounced on top of watchfiles' own 200 ms. Keyed on the workbook and never
on "any save", which matters because ``.workbench/`` is **not** in
``IGNORED_DIRS``: every file the app writes under it already publishes a change
event, so a trigger on any save would let an evidence write re-fire the gate that
produced it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.machinery
import json
import os
import re
import sys
import time
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import BaseModel, ValidationError

from workbench_server.models.files import FileChangedEvent
from workbench_server.models.reconcile_spec import (
    ABSENT_DIGEST,
    MAX_CHECKS_PER_SPEC,
    MAX_PAIRS_PER_CHECK,
    MAX_SPEC_BYTES,
    MAX_SPEC_LOG_BYTES,
    MAX_SPEC_STDOUT_BYTES,
    MAX_SPECS,
    SETTING_FAKE,
    SETTING_TIMEOUT,
    SPEC_DIR,
    SPEC_SUFFIX,
    CoveredSource,
    ReconcileSpecFile,
    SpecApproval,
    SpecCheck,
    SpecEntryRequest,
    SpecEntryResult,
    SpecEvent,
    SpecOutcome,
    SpecRunReport,
    SpecState,
    SpecStates,
    SpecStatus,
    SpecValue,
)
from workbench_server.models.reconciliation import (
    ExpectedValue,
    ReconciliationSpec,
    TimeExpectation,
    TimeIndexSpec,
    Tolerance,
)
from workbench_server.models.validation import (
    EvidenceTruncation,
    RiskLevel,
    ValidationResult,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.app_data import app_data_dir
from workbench_server.services.event_bus import EventBus
from workbench_server.services.gates import _BoundedCapture

log = structlog.get_logger()

#: The **fixed, server-owned** argv. Nothing a spec file says reaches it: the
#: spec travels on stdin as JSON, and the module it starts is ours. This constant
#: is what ``test_reconcile_spec.py`` pins, because "the argv is fixed" is a
#: claim a later edit could quietly break.
SPEC_ENTRY_ARGV: tuple[str, ...] = ("uv", "run", "python", "-m", "workbench_server.spec_entry")

#: Default ceiling on one spec's subprocess, in seconds. Named in every refusal
#: it would fix (:data:`SETTING_TIMEOUT`).
DEFAULT_TIMEOUT_S = 60.0

#: How long after a workbook save the gate waits before running, on top of
#: watchfiles' own 200 ms. Long enough that Excel's write-temp-then-rename dance
#: is one run rather than three.
DEFAULT_DEBOUNCE_S = 0.5

#: Bytes pulled off a pipe per read while the child runs.
_READ_CHUNK = 8_192

#: The approval file under the machine's app-data dir. **Not** in ``.workbench/``:
#: the reason ``RecentsStore`` spells out, sharpened by what this one authorises —
#: a trust record inside the folder it authorises is one an attacker can write.
APPROVALS_FILE = "reconcile-approvals.json"

#: Version stamp on that document, so a later shape change can be read rather
#: than guessed at.
APPROVALS_VERSION = 1

#: A two-column A1 range: ``Hours!A2:B8761`` or ``A2:B8761``.
_RANGE_RE = re.compile(
    r"^(?:(?P<sheet>[^!]+)!)?(?P<c1>[A-Za-z]{1,3})(?P<r1>\d+):(?P<c2>[A-Za-z]{1,3})(?P<r2>\d+)$"
)

#: Module names are dotted identifiers and nothing else — no separators, no
#: parent references. Checked before a name is turned into a path.
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: risk → the spec's own word for it.
_RISK_OUTCOME: dict[RiskLevel, SpecOutcome] = {
    "pass": "pass",
    "low": "skipped",
    "medium": "warn",
    "high": "fail",
    "blocked": "blocked",
}


class SpecProblem(Exception):
    """A spec that cannot be used, with the sentence a reader needs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SpecConflict(Exception):
    """An approval whose digest no longer matches what is on disk."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---- digests -----------------------------------------------------------------


def file_digest(path: Path) -> str:
    """``blake2b`` of a file's bytes, or :data:`ABSENT_DIGEST`.

    Absent rather than an omission, and the distinction is load-bearing: an
    approval covering a module that is *not there yet* must be revoked the moment
    somebody creates it, or "approve while the file is missing" would be a way to
    pre-authorise code nobody has written. Same rule as
    ``services/gates.py::content_digest``, one level up.
    """
    digest = hashlib.blake2b(digest_size=16)
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 16):
                digest.update(chunk)
    except OSError:
        return ABSENT_DIGEST
    return digest.hexdigest()


def composite_digest(name: str, sources: Sequence[CoveredSource]) -> str:
    """The one number an approval is keyed to.

    Folds the spec's name and then every covered source's ``(origin, path,
    digest)`` in the order the list carries. Order is part of the key on purpose:
    the list is built deterministically (spec first, then callables, then the
    closure, each sorted), so a reordering can only mean the set changed.
    """
    digest = hashlib.blake2b(digest_size=16)
    digest.update(name.encode("utf-8", errors="replace"))
    for source in sources:
        digest.update(b"\0")
        digest.update(f"{source.origin}\0{source.path}\0{source.digest}".encode())
    return digest.hexdigest()


# ---- resolving a callable to a file ------------------------------------------


def resolve_module(root: Path, module: str) -> Path | None:
    """The file ``module`` names under ``root``, existing or not.

    Returns the *candidate* path even when nothing is there, because "the module
    is missing" is a state the approval has to be able to record (see
    :func:`file_digest`). ``None`` means the name is not one that can address a
    file under the root at all — which is refused rather than resolved to
    something plausible.
    """
    if not _MODULE_RE.match(module):
        return None
    parts = module.split(".")
    package = root.joinpath(*parts) / "__init__.py"
    flat = root.joinpath(*parts).with_suffix(".py")
    for candidate in (flat, package):
        if candidate.is_file():
            return candidate
    return flat


def _relative(root: Path, path: Path) -> str | None:
    """``path`` as a workspace-relative POSIX string, or ``None`` outside it."""
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _inside(root: Path, where: str | None) -> bool:
    """Is ``where`` a real path under ``root``?

    ``exists()`` rather than a pure string comparison because a
    :class:`~importlib.machinery.ModuleSpec` origin is not always a path:
    ``"built-in"`` and ``"frozen"`` would otherwise resolve *relative to the
    process's cwd* and read as in-tree, which is the answer that would let the
    stdlib through the check below.
    """
    if where is None:
        return False
    try:
        path = Path(where).resolve()
        if not path.exists():
            return False
    except (OSError, ValueError):
        return False
    return path == root or root in path.parents


def _import_origin(root: Path, top: str) -> str | None:
    """Where Python's own machinery answers ``top`` from, when that is *outside*
    ``root``. ``None`` when nothing answers it, or the answer is in-tree.

    Deliberately **import-free**. :class:`importlib.machinery.PathFinder`
    consults the path hooks and hands back a spec without executing a line of the
    module it found, which is the only acceptable way to ask this question from a
    listing endpoint whose whole promise is that reading it runs nothing. The
    server's own ``sys.path`` is an approximation of the child's — it is the
    stdlib and the installed distributions that matter here, and those are the
    same set — and a false *negative* only leaves the behaviour that shipped.
    """
    if top in sys.builtin_module_names:
        return "a built-in module"
    try:
        found = importlib.machinery.PathFinder.find_spec(top, sys.path)
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return None
    if found is None:
        return None
    where = [found.origin] if found.origin is not None else []
    where.extend(found.submodule_search_locations or [])
    outside = [place for place in where if not _inside(root, place)]
    return outside[0] if outside else None


def module_outside_workspace(root: Path, module: str) -> str | None:
    """Why this ``module`` cannot be the workspace's own code, or ``None``.

    The contract a ``callable`` states — and the one the panel repeats — is
    *module:function within the workspace*, and until this function existed
    nothing held it. :func:`resolve_module` hands back a fabricated in-tree
    candidate for **any** dotted name, so a spec naming an *installed*
    distribution recorded ``root/<name>.py`` at :data:`ABSENT_DIGEST`; that
    phantom path is never created, so :meth:`ReconcileSpecService._rehash`
    recomputes the same "absent" digest forever and the approval can never go
    stale. Meanwhile ``spec_entry.call_one`` imports the name through
    ``sys.path``, so the code that really runs is whatever a ``uv sync`` last
    put in ``site-packages``. That is the exact defect the composite digest
    exists to prevent — an approval authorising code it never hashed — reached
    by a different door.

    So the two "there is no file at that path" cases are told apart:

    * **not written yet** — the name addresses a place *inside* the workspace,
      or nothing on the import path answers to it. ``absent`` is recorded and
      the approval is revoked the moment somebody writes the file, which is what
      ``test_a_missing_module_is_covered_as_absent_not_omitted`` pins.
    * **shadowed** — the stdlib or an installed distribution answers the
      top-level name today. Refused here, by name, at load time: the spec is
      ``invalid``, it cannot be approved, and it runs nothing.
    """
    resolved = resolve_module(root, module)
    if resolved is None:
        return (
            f"`callable` names {module!r}, which is not a module name — a callable is "
            "`module:function` in this workspace's own code."
        )
    if resolved.is_file():
        return None
    top = module.partition(".")[0]
    if (root / top).is_dir() or (root / f"{top}.py").is_file():
        return None
    origin = _import_origin(root, top)
    if origin is None:
        return None
    return (
        f"`callable` names {module!r}, which is not this workspace's code: Python resolves "
        f"{top!r} to {origin}. A spec may only name a module under the workspace root — an "
        "approval keyed to a file that is not there could never go stale when the code that "
        "actually runs changes."
    )


# ---- the runner seam ---------------------------------------------------------


@dataclass(frozen=True)
class SpecRunOutput:
    """One subprocess run: what it answered, and everything it said on the way."""

    #: The parsed envelope, or ``None`` when the child never produced one —
    #: killed at the ceiling, could not start, or wrote something unparseable.
    result: SpecEntryResult | None
    #: ``None`` covers both "killed at the ceiling" and "never started"; the log's
    #: head carries which, which is what the head half of the window is for.
    exit_code: int | None
    duration_ms: int
    #: The child's stderr — its tracebacks and anything the callable printed.
    log: str
    truncated: EvidenceTruncation | None = None


class SpecRunner(Protocol):
    """Run one spec's callables in one directory and hand back their values.

    Two implementations, the gates/Office-host split: the real one spawns a
    process, the fake one reads a script.
    """

    async def run(
        self, request: SpecEntryRequest, cwd: Path, timeout_s: float, window: int
    ) -> SpecRunOutput: ...


class SubprocessSpecRunner:
    """The real one: one process, bounded, with nothing to prompt at.

    ``create_subprocess_exec``, never ``shell=True`` — :data:`SPEC_ENTRY_ARGV` is
    passed through as separate arguments, so there is no string for a shell to
    reinterpret, and the spec goes down **stdin**.

    ``argv`` is a constructor argument only so the *process-handling* half (the
    hard timeout, the bounded capture, a child that cannot start) is testable
    without paying ``uv``'s resolution on every assertion. It is never assembled
    from a request — ``test_reconcile_spec.py`` pins the shipped default.
    """

    def __init__(self, argv: Sequence[str] = SPEC_ENTRY_ARGV) -> None:
        self._argv = tuple(argv)

    @property
    def argv(self) -> tuple[str, ...]:
        return self._argv

    async def run(
        self, request: SpecEntryRequest, cwd: Path, timeout_s: float, window: int
    ) -> SpecRunOutput:
        started = time.monotonic()
        errors = _BoundedCapture(window)
        payload = request.model_dump_json().encode()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
        except OSError as err:
            errors.feed(f"could not start {self._argv[0]!r}: {err}".encode())
            return self._output(None, None, started, errors)
        stdout = _BoundedCapture(MAX_SPEC_STDOUT_BYTES)
        try:
            await asyncio.wait_for(self._drive(proc, payload, stdout, errors), timeout_s)
        except TimeoutError:
            await self._kill(proc)
            errors.feed(f"\n[killed: no exit after {timeout_s:.0f}s]".encode())
            return self._output(None, None, started, errors)
        except asyncio.CancelledError:
            # Shutdown, or a re-root, reaching a run that is *already spawned*.
            # Without this branch the cancellation unwinds straight out of here
            # with the child still running the analyst's code in their own
            # folder, unsupervised, with nothing left holding a handle to it —
            # the orphan the timeout branch above was written to prevent, by the
            # one route that skips it. Kill, reap, then let the cancellation
            # continue: a shutdown that swallowed it would never finish.
            await self._kill(proc)
            raise
        envelope, _ = stdout.render()
        return self._output(envelope, proc.returncode, started, errors)

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process) -> None:
        """Kill the child and reap it.

        The reap is suppressed against a *second* cancellation rather than
        skipped: the kill has already been delivered by then, so the child is
        gone either way, and a shutdown must not hang waiting for a wait that
        will be interrupted again.
        """
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(asyncio.CancelledError):
            await proc.wait()

    @staticmethod
    def _env() -> dict[str, str]:
        """The child's environment.

        ``PYTHONPATH`` gains the *server's* own ``src`` so ``-m
        workbench_server.spec_entry`` resolves even in a workspace whose virtual
        environment has never heard of this application — which is every
        workspace but this one. It is **appended after** the interpreter's own
        ``sys.path[0]`` (the cwd, which ``-m`` puts first), so the workspace's own
        modules still win any name collision: the analyst's code is the code
        being run, and the harness must never shadow it.
        """
        source_root = Path(__file__).resolve().parents[2]
        existing = os.environ.get("PYTHONPATH", "")
        joined = f"{source_root}{os.pathsep}{existing}" if existing else str(source_root)
        return dict(
            os.environ,
            PYTHONPATH=joined,
            GIT_TERMINAL_PROMPT="0",
            NO_COLOR="1",
            PYTHONUNBUFFERED="1",
        )

    @staticmethod
    async def _drive(
        proc: asyncio.subprocess.Process,
        payload: bytes,
        stdout: _BoundedCapture,
        errors: _BoundedCapture,
    ) -> None:
        """Feed stdin, drain both pipes concurrently, wait.

        Both pipes at once and not one after the other: a child that fills its
        stderr buffer while the parent is still reading stdout deadlocks, and a
        callable that prints is the *normal* case here rather than an exotic one.
        """

        async def feed() -> None:
            writer = proc.stdin
            if writer is None:  # pragma: no cover - PIPE is always requested
                return
            writer.write(payload)
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                await writer.drain()
                writer.close()

        async def drain(stream: asyncio.StreamReader | None, into: _BoundedCapture) -> None:
            if stream is None:  # pragma: no cover - PIPE is always requested
                return
            while chunk := await stream.read(_READ_CHUNK):
                into.feed(chunk)

        await asyncio.gather(feed(), drain(proc.stdout, stdout), drain(proc.stderr, errors))
        await proc.wait()

    @staticmethod
    def _output(
        envelope: str | None, code: int | None, started: float, errors: _BoundedCapture
    ) -> SpecRunOutput:
        text, truncated = errors.render()
        duration_ms = round((time.monotonic() - started) * 1000)
        parsed = parse_envelope(envelope) if envelope is not None else None
        if envelope is not None and parsed is None:
            text = f"{text}\n[stdout was not a spec envelope]"
        return SpecRunOutput(
            result=parsed,
            exit_code=code,
            duration_ms=duration_ms,
            log=text,
            truncated=truncated,
        )


def parse_envelope(raw: str) -> SpecEntryResult | None:
    """The child's last JSON line as a typed envelope, or ``None``.

    The *last* line rather than the whole buffer: the entry module writes exactly
    one, but a workspace whose ``sitecustomize`` prints a banner would otherwise
    make a perfectly good run unparseable.
    """
    for line in reversed(raw.strip().splitlines()):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            return SpecEntryResult.model_validate_json(candidate)
        except ValidationError:
            return None
    return None


#: What a fake callable answers when its name says nothing else. The E2E fixture
#: puts exactly this number in the workbook, so the loop is green until somebody
#: writes a wrong one — which is the assertion PR-B exists to make.
FAKE_DEFAULT_VALUE = 1234.5

#: Function-name triggers, the ``FakeDocumentBridge`` precedent: the *name* of the
#: thing scripts the behaviour, so a fixture is a file name rather than a knob.
_FAKE_RAISES = "raises"
_FAKE_HANGS = "hangs"
_FAKE_CHATTY = "chatty"

#: How much a ``chatty`` fake prints, so the bounded-capture branch is proven
#: without a real process.
_FAKE_CHATTER_BYTES = 64 * 1024

_FAKE_VALUE_RE = re.compile(r"_v(\d+(?:_\d+)?)$")


class FakeSpecRunner:
    """Scripted values, no process anywhere — the ``WORKBENCH_OFFICE_FAKE`` posture.

    Off by default and loudly logged when on, because a chip claiming a workbook
    reconciles against code that never ran would be a worse lie than a chip that
    fails.

    The default script is **name-driven**, exactly as ``FakeDocumentBridge`` mints
    content from a document's name: a callable whose function name contains
    ``raises`` fails, one containing ``hangs`` never produces an envelope (the
    timeout shape), one containing ``chatty`` prints past the window, a
    ``…_v<digits>`` suffix answers with that number, and everything else answers
    :data:`FAKE_DEFAULT_VALUE`. Tests that want something else pass ``script``.
    """

    def __init__(
        self,
        script: Mapping[str, SpecValue] | None = None,
        *,
        modules: Sequence[str] = (),
        delay_s: float = 0.0,
    ) -> None:
        self._script = dict(script or {})
        self._modules = list(modules)
        self._delay_s = delay_s
        #: Every request this runner was handed, so a test can assert **zero**
        #: calls — which is how "an unapproved spec runs nothing" is proven.
        self.calls: list[SpecEntryRequest] = []

    async def run(
        self, request: SpecEntryRequest, cwd: Path, timeout_s: float, window: int
    ) -> SpecRunOutput:
        del cwd  # nothing is spawned, so there is no directory to spawn it in
        self.calls.append(request)
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        errors = _BoundedCapture(window)
        values: list[SpecValue] = []
        for reference in request.callables:
            function = reference.partition(":")[2]
            if _FAKE_HANGS in function:
                errors.feed(f"\n[killed: no exit after {timeout_s:.0f}s]".encode())
                text, truncated = errors.render()
                return SpecRunOutput(
                    result=None, exit_code=None, duration_ms=1, log=text, truncated=truncated
                )
            if _FAKE_CHATTY in function:
                errors.feed(b"x" * _FAKE_CHATTER_BYTES)
            values.append(self._value_for(reference, function))
        text, truncated = errors.render()
        return SpecRunOutput(
            result=SpecEntryResult(ok=True, values=values, modules=list(self._modules)),
            exit_code=0,
            duration_ms=1,
            log=text,
            truncated=truncated,
        )

    def _value_for(self, reference: str, function: str) -> SpecValue:
        scripted = self._script.get(reference)
        if scripted is not None:
            return scripted
        if _FAKE_RAISES in function:
            return SpecValue(
                call=reference,
                ok=False,
                error=f"{reference} raised — RuntimeError: scripted failure (fake)",
            )
        match = _FAKE_VALUE_RE.search(function)
        value = float(match.group(1).replace("_", ".")) if match else FAKE_DEFAULT_VALUE
        return SpecValue(call=reference, ok=True, scalar=value)


def build_spec_runner(fake: bool) -> SpecRunner:
    """The one construction point, so ``main.py`` states the choice once."""
    if fake:
        log.warning(
            "reconcile_spec.fake_mode_enabled",
            detail=f"{SETTING_FAKE} is set: spec values are scripted, no user code runs",
        )
        return FakeSpecRunner()
    return SubprocessSpecRunner()


# ---- the approval record -----------------------------------------------------


class SpecApprovalStore:
    """Approvals on disk, under the machine's app-data dir. Never raises.

    Losing this file costs the user one trust prompt they can answer again; it
    must never cost them a server that will not start, so every failure resolves
    to "nothing approved, plus a sentence saying why" — the ``RecentsStore``
    posture. Failing *closed* is also the safe direction here: an unreadable
    approval file means nothing runs, not that everything does.
    """

    def __init__(self, directory: Path | None = None) -> None:
        self._path = (directory or app_data_dir()) / APPROVALS_FILE
        self._problem: str | None = None
        self._records: dict[tuple[str, str], SpecApproval] = {}
        self._roots: dict[str, str] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def problem(self) -> str | None:
        self._ensure_loaded()
        return self._problem

    @staticmethod
    def _key(root: Path, name: str) -> tuple[str, str]:
        """Identity of one approval. The root is case-folded because Windows
        paths are, and ``C:\\Work`` and ``c:\\work`` being two trust records is
        the kind of small wrongness that reads as a broken app."""
        return os.path.normcase(str(root)), name

    def get(self, root: Path, name: str) -> SpecApproval | None:
        self._ensure_loaded()
        return self._records.get(self._key(root, name))

    def put(self, root: Path, approval: SpecApproval) -> None:
        self._ensure_loaded()
        key = self._key(root, approval.name)
        self._records[key] = approval
        self._roots[key[0]] = str(root)
        self._save()

    def drop(self, root: Path, name: str) -> None:
        self._ensure_loaded()
        if self._records.pop(self._key(root, name), None) is not None:
            self._save()

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            self._problem = f"could not read {self._path}: {exc}"
            log.warning("reconcile_spec.approvals_unreadable", path=str(self._path), error=str(exc))
            return
        try:
            document = json.loads(raw)
            entries = document["approvals"]
        except (ValueError, KeyError, TypeError) as exc:
            self._problem = f"{self._path} is not a readable approvals document: {exc}"
            log.warning("reconcile_spec.approvals_corrupt", path=str(self._path), error=str(exc))
            return
        for entry in entries if isinstance(entries, list) else []:
            try:
                root = str(entry["root"])
                approval = SpecApproval.model_validate(entry["approval"])
            except (ValidationError, KeyError, TypeError):
                # One unreadable record costs one approval, not the file. The
                # `services/layouts.py` posture: losing a trust decision costs a
                # prompt, and guessing at one costs a run nobody authorised.
                log.warning("reconcile_spec.approval_line_skipped", path=str(self._path))
                continue
            key = (os.path.normcase(root), approval.name)
            self._records[key] = approval
            self._roots[key[0]] = root

    def _save(self) -> None:
        document = {
            "version": APPROVALS_VERSION,
            "approvals": [
                {
                    "root": self._roots.get(key[0], key[0]),
                    "approval": approval.model_dump(mode="json"),
                }
                for key, approval in sorted(self._records.items())
            ],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(document, indent=2), encoding="utf-8")
        except OSError as exc:
            self._problem = f"could not write {self._path}: {exc}"
            log.warning("reconcile_spec.approvals_unwritable", path=str(self._path), error=str(exc))


# ---- what the service needs from the validation frame -------------------------


class ValidationRunner(Protocol):
    """The one thing this module asks of ``ValidationService``.

    A narrow Protocol rather than the class, the ``SlotLocator`` pattern: this
    module produces a ``ReconciliationSpec`` and hands it to the gate that
    already exists, and it has no business reaching anything else on that service.
    """

    async def run(self, spec: ValidationSpec) -> ValidationResult: ...


@dataclass
class _Loaded:
    """A spec file, as far as it could be read."""

    name: str
    path: Path
    relative: str
    spec: ReconcileSpecFile | None = None
    problem: str | None = None
    sources: list[CoveredSource] = field(default_factory=list)


class ReconcileSpecService:
    """The specs in a workspace: what they are, whether they may run, and the
    loop that runs them when a workbook is saved."""

    def __init__(
        self,
        root: Path,
        bus: EventBus,
        runner: SpecRunner,
        validation: ValidationRunner,
        store: SpecApprovalStore,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        clock: Callable[[], datetime] = lambda: datetime.now().replace(microsecond=0),
    ) -> None:
        self._root = root.resolve()
        self._bus = bus
        self._runner = runner
        self._validation = validation
        self._store = store
        self._timeout_s = timeout_s
        self._debounce_s = debounce_s
        self._clock = clock
        self._last_runs: dict[str, SpecRunReport] = {}
        #: Debouncing runs, keyed by workbook — the ones a *newer save* of the
        #: same workbook supersedes. A task leaves this map the moment it is past
        #: its debounce, which is why it cannot be the only handle on a run.
        self._pending: dict[str, asyncio.Task[None]] = {}
        #: Every scheduled run until it returns, debouncing **or in flight**.
        #: ``stop()`` cancels these, and a run cancelled mid-flight is what kills
        #: and reaps its subprocess; ``_pending`` alone could only ever reach one
        #: still asleep in the debounce window, which is precisely the run with
        #: no child to leave behind.
        self._running: set[asyncio.Task[None]] = set()
        self._queue: asyncio.Queue[BaseModel] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Subscribe to the bus the watcher publishes on. Publishing back onto
        the same bus is safe: our own frames are not ``FileChangedEvent``."""
        self._queue = self._bus.subscribe()
        self._task = asyncio.create_task(self._consume(), name="reconcile-spec")

    async def stop(self) -> None:
        await self._cancel_scheduled()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._queue is not None:
            self._bus.unsubscribe(self._queue)
            self._queue = None

    async def _cancel_scheduled(self) -> None:
        """Cancel every scheduled run — debouncing *and* in flight — and wait.

        The waiting is the whole point, and it is what ``main.py``'s shutdown
        comment has always claimed: a task cancelled mid-run is what reaches
        :meth:`SubprocessSpecRunner._kill`, and a shutdown that cancelled without
        awaiting would return before the child was killed, leaving it running in
        the user's folder with nothing left to supervise it.
        """
        self._pending.clear()
        scheduled = list(self._running)
        self._running.clear()
        for task in scheduled:
            task.cancel()
        if scheduled:
            await asyncio.gather(*scheduled, return_exceptions=True)

    def set_workspace_root(self, root: Path) -> None:
        """Re-root, and forget every *run*.

        The runs are keyed by a spec name that means something different in the
        project the user just opened. The **approvals** are not forgotten and must
        not be: they live under the machine's app-data dir keyed by the root they
        were made for, so returning to a project returns to the decisions made
        there — which is the whole reason they are not in ``.workbench/``.
        """
        self._root = root.resolve()
        self._last_runs.clear()
        # In-flight runs too, and for the same reason ``stop()`` cancels them: a
        # subprocess spawned against the folder the user just left has nothing
        # left to judge. This one cannot await the reap — ``set_workspace_root``
        # is synchronous by contract — but the cancellation still reaches
        # ``SubprocessSpecRunner``'s kill.
        for task in list(self._running):
            task.cancel()
        self._running.clear()
        self._pending.clear()

    # ---- reading the folder -------------------------------------------------

    @property
    def directory(self) -> Path:
        return self._root / SPEC_DIR

    def _spec_files(self) -> list[Path]:
        try:
            found = sorted(p for p in self.directory.iterdir() if p.suffix == SPEC_SUFFIX)
        except OSError:
            return []
        return found[:MAX_SPECS]

    def _load(self, path: Path) -> _Loaded:
        """One spec file, read as far as it goes.

        A file that cannot be parsed is still a *row*, with the reason on it. A
        spec silently missing from the list is how somebody comes to believe a
        gate is running that never was.
        """
        name = path.stem
        relative = _relative(self._root, path) or path.name
        loaded = _Loaded(name=name, path=path, relative=relative)
        try:
            size = path.stat().st_size
        except OSError as exc:
            loaded.problem = f"could not read {relative}: {exc}"
            return self._with_sources(loaded)
        if size > MAX_SPEC_BYTES:
            loaded.problem = (
                f"{relative} is {size} bytes; a spec is capped at {MAX_SPEC_BYTES} — "
                "this is a file that wandered into the folder, not a spec that got long"
            )
            return self._with_sources(loaded)
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            loaded.problem = f"{relative} is not readable TOML: {exc}"
            return self._with_sources(loaded)
        try:
            loaded.spec = ReconcileSpecFile.model_validate(document)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"]) or "spec"
            loaded.problem = f"{relative}: {where} — {first['msg']}"
            return self._with_sources(loaded)
        if not loaded.spec.checks:
            loaded.problem = f"{relative} declares no [[check]] — there is nothing to prove"
            return self._with_sources(loaded)
        if sum(1 for check in loaded.spec.checks if check.range is not None) > 1:
            loaded.problem = (
                f"{relative} declares more than one `range` check; a spec compiles into one "
                "ReconciliationSpec, which carries one time index. Split the ranges across "
                "one spec file each"
            )
            return self._with_sources(loaded)
        shadowed = next(
            (
                reason
                for reason in (
                    module_outside_workspace(self._root, check.call.partition(":")[0])
                    for check in loaded.spec.checks
                )
                if reason is not None
            ),
            None,
        )
        if shadowed is not None:
            # Refused at *load* rather than at run: an approval over a name the
            # import system answers from `site-packages` is keyed to a file that
            # does not exist, so its digest can never move — a dependency bump
            # under it would be invisible to the mechanism written to catch an
            # edit. `spec_entry.py` refuses the same name again on its own side.
            loaded.problem = f"{relative}: {shadowed}"
            return self._with_sources(loaded)
        if self._workbook_path(loaded.spec.workbook) is None:
            loaded.problem = (
                f"{relative}: workbook path escapes the workspace ({loaded.spec.workbook!r})"
            )
        return self._with_sources(loaded)

    def _with_sources(self, loaded: _Loaded) -> _Loaded:
        """The entry points this spec would be approved over: its own bytes,
        then one line per module a ``callable`` resolves to."""
        sources = [
            CoveredSource(path=loaded.relative, digest=file_digest(loaded.path), origin="spec")
        ]
        seen: set[str] = set()
        for check in loaded.spec.checks if loaded.spec is not None else []:
            module = check.call.partition(":")[0]
            resolved = resolve_module(self._root, module)
            relative = None if resolved is None else _relative(self._root, resolved)
            if relative is None:
                # A module name that cannot address a file under the root is
                # named as its own covered line rather than dropped: an approval
                # that quietly covered nothing would be the shadow again.
                relative = f"<unresolvable module {module}>"
                digest = ABSENT_DIGEST
            else:
                digest = file_digest(self._root / relative)
            if relative in seen:
                continue
            seen.add(relative)
            sources.append(CoveredSource(path=relative, digest=digest, origin="callable"))
        loaded.sources = [sources[0], *sorted(sources[1:], key=lambda s: s.path)]
        return loaded

    def _workbook_path(self, relative: str) -> Path | None:
        """The workbook, jailed against the workspace root."""
        candidate = (self._root / relative).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            return None
        return candidate

    # ---- state --------------------------------------------------------------

    def _rehash(self, covered: Sequence[CoveredSource]) -> list[CoveredSource]:
        """The same covered list, digested against what is on disk **now**."""
        return [
            source.model_copy(update={"digest": file_digest(self._root / source.path)})
            if not source.path.startswith("<")
            else source
            for source in covered
        ]

    def _state_of(self, loaded: _Loaded) -> SpecState:
        approval = self._store.get(self._root, loaded.name)
        fresh_digest = composite_digest(loaded.name, loaded.sources)
        if loaded.problem is not None:
            return SpecState(
                name=loaded.name,
                path=loaded.relative,
                workbook="" if loaded.spec is None else loaded.spec.workbook,
                status="invalid",
                digest=fresh_digest,
                checks=0 if loaded.spec is None else len(loaded.spec.checks),
                approval=approval,
                last_run=self._last_runs.get(loaded.name),
                detail=loaded.problem,
            )
        spec = loaded.spec
        assert spec is not None  # noqa: S101 - `problem is None` implies a parsed spec
        checks = len(spec.checks)
        if approval is None:
            return SpecState(
                name=loaded.name,
                path=loaded.relative,
                workbook=spec.workbook,
                status="unapproved",
                digest=fresh_digest,
                checks=checks,
                approval=None,
                # The receipt for a decision nobody has taken yet — the list the
                # panel puts *beside* the Approve button, so the sentence below
                # is checkable before the click rather than only after it.
                pending_covered=list(loaded.sources),
                last_run=self._last_runs.get(loaded.name),
                detail=(
                    f"Not approved. Approving runs {checks} callable(s) from this workspace's own "
                    f"code whenever {spec.workbook} is saved — this spec, running exactly this "
                    "code, on this machine, until either changes."
                ),
            )
        current = self._rehash(approval.covered)
        if composite_digest(loaded.name, current) == approval.digest:
            return SpecState(
                name=loaded.name,
                path=loaded.relative,
                workbook=spec.workbook,
                status="approved",
                digest=approval.digest,
                checks=checks,
                approval=approval,
                last_run=self._last_runs.get(loaded.name),
                detail=(
                    f"Approved by {approval.approver}; re-runs on every save of {spec.workbook}. "
                    f"Covers {len(approval.covered)} file(s)."
                ),
            )
        changed = _changed_paths(approval.covered, current)
        return SpecState(
            name=loaded.name,
            path=loaded.relative,
            workbook=spec.workbook,
            status="stale",
            digest=fresh_digest,
            checks=checks,
            approval=approval,
            # Both lists ride a stale row on purpose: `approval.covered` is what
            # was trusted, `pending_covered` is what "Approve again" would trust.
            # The difference between them is the decision being asked for.
            pending_covered=list(loaded.sources),
            last_run=self._last_runs.get(loaded.name),
            detail=_stale_detail(changed),
        )

    def states(self) -> SpecStates:
        loaded = [self._load(path) for path in self._spec_files()]
        specs = [self._state_of(entry) for entry in loaded]
        if not specs:
            return SpecStates(
                specs=[],
                detail=(
                    f"No reconciliation specs. Create {SPEC_DIR.as_posix()}/<name>.toml naming a "
                    "workbook, a cell or range, and a callable in this workspace's own code; "
                    "approve it once and it re-runs on every save."
                ),
            )
        counts: dict[SpecStatus, int] = {}
        for state in specs:
            counts[state.status] = counts.get(state.status, 0) + 1
        parts = [
            f"{counts[s]} {s}"
            for s in ("invalid", "stale", "unapproved", "approved")
            if counts.get(s)
        ]
        return SpecStates(specs=specs, detail=f"{len(specs)} spec(s): {', '.join(parts)}.")

    def state(self, name: str) -> SpecState | None:
        path = self.directory / f"{name}{SPEC_SUFFIX}"
        if not path.is_file():
            return None
        return self._state_of(self._load(path))

    # ---- the trust decision -------------------------------------------------

    def approve(self, name: str, approver: str, digest: str) -> SpecState:
        """Record the one-time decision, keyed to the code it names.

        The caller echoes the digest it was shown. A digest that no longer
        matches is refused: approving a spec whose bytes moved under the dialog
        would approve something nobody read.
        """
        path = self.directory / f"{name}{SPEC_SUFFIX}"
        if not path.is_file():
            raise SpecProblem(f"no spec named {name!r} in {SPEC_DIR.as_posix()}")
        loaded = self._load(path)
        if loaded.problem is not None:
            raise SpecProblem(loaded.problem)
        current = self._state_of(loaded)
        if digest != current.digest:
            raise SpecConflict(
                f"{name} changed since it was shown (expected {digest[:12]}…, now "
                f"{current.digest[:12]}…). Re-read the spec and approve again — an approval "
                "that covered bytes nobody looked at is not a trust decision."
            )
        covered = (
            self._rehash(current.approval.covered)
            if current.status == "approved" and current.approval is not None
            else loaded.sources
        )
        approval = SpecApproval(
            name=name,
            digest=composite_digest(name, covered),
            approver=approver,
            approved_at=self._clock(),
            covered=list(covered),
        )
        self._store.put(self._root, approval)
        log.info(
            "reconcile_spec.approved",
            spec=name,
            approver=approver,
            covered=len(covered),
            digest=approval.digest[:12],
        )
        return self._publish(self._state_of(self._load(path)))

    def revoke(self, name: str) -> SpecState | None:
        """Withdraw the decision. The spec stays; it simply runs nothing again
        until somebody says so."""
        self._store.drop(self._root, name)
        log.info("reconcile_spec.revoked", spec=name)
        state = self.state(name)
        return None if state is None else self._publish(state)

    # ---- running ------------------------------------------------------------

    async def run(self, name: str, *, trigger: str = "manual") -> SpecRunReport:
        """Run one spec: verify the digest, spawn, compile, hand to the gate.

        Serialised across specs on purpose. Two runs at once would be two
        subprocesses in the user's own workspace root, and the gain — a second or
        two on a save nobody is watching — is not worth the surprise.
        """
        async with self._lock:
            return await self._run_locked(name, trigger)

    async def _run_locked(self, name: str, trigger: str) -> SpecRunReport:
        started = time.monotonic()
        ran_at = self._clock()
        path = self.directory / f"{name}{SPEC_SUFFIX}"
        if not path.is_file():
            return self._record(
                name,
                "blocked",
                trigger,
                ran_at,
                started,
                detail=f"no spec named {name!r} in {SPEC_DIR.as_posix()} — nothing was run.",
            )
        loaded = await asyncio.to_thread(self._load, path)
        state = self._state_of(loaded)
        if state.status != "approved":
            # The refusal is the feature. **Nothing is spawned** on this path —
            # `test_reconcile_spec.py` asserts the runner saw zero calls — which
            # is what "no spec ever runs on folder-open" means in code.
            return self._record(
                name,
                "blocked",
                trigger,
                ran_at,
                started,
                detail=f"{state.detail} Nothing was run.",
            )
        spec = loaded.spec
        assert spec is not None  # noqa: S101 - an approved state implies a parsed spec
        request = SpecEntryRequest(
            callables=[check.call for check in spec.checks][:MAX_CHECKS_PER_SPEC],
            max_pairs=MAX_PAIRS_PER_CHECK,
        )
        output = await self._runner.run(request, self._root, self._timeout_s, MAX_SPEC_LOG_BYTES)
        if output.result is None:
            return self._record(
                name,
                "fail",
                trigger,
                ran_at,
                started,
                detail=(
                    f"the spec process produced no result after {output.duration_ms} ms — it timed "
                    f"out, could not start, or wrote something that is not a spec envelope. Raise "
                    f"{SETTING_TIMEOUT} if these callables need longer. Last output: "
                    f"{_one_line(output.log)}"
                ),
            )
        compiled, problems = compile_spec(spec, output.result)
        if compiled is None:
            return self._record(
                name,
                "fail",
                trigger,
                ran_at,
                started,
                detail=f"no expected values were produced: {'; '.join(problems)}",
            )
        result = await self._validation.run(
            ValidationSpec(
                subject=ValidationSubject(kind="file", ref=spec.workbook, label=spec.workbook),
                checks=["reconciliation"],
                params=compiled.model_dump(),
            )
        )
        self._extend_approval(name, output.result.modules)
        values = len(compiled.expectations) + (
            0 if compiled.time_index is None else len(compiled.time_index.expectations)
        )
        detail = (
            f"{headline(result)} ({values} expected value(s) from {len(spec.checks)} callable(s); "
            f"risk {result.risk})"
        )
        if problems:
            detail = f"{detail} {len(problems)} callable(s) produced nothing: {'; '.join(problems)}"
        outcome = _RISK_OUTCOME[result.risk] if not problems else _worse(_RISK_OUTCOME[result.risk])
        return self._record(
            name,
            outcome,
            trigger,
            ran_at,
            started,
            detail=detail,
            validation_id=result.validation_id,
            values=values,
        )

    def _extend_approval(self, name: str, modules: Iterable[str]) -> None:
        """Fold the run's real import closure into the approval, and re-key it.

        This is the second half of the composite digest and the reason it is not
        a guess: after the callables returned, the child walked ``sys.modules``
        and named every workspace-local file that actually participated. Adding
        them here is what makes the *next* run refuse when a helper the callable
        pulled its arithmetic from is edited — the case hashing the spec and its
        entry modules cannot see.

        Extending is authorised, not a revocation: these are the bytes that just
        ran under the approval that was given. From the second run on, changing
        any of them revokes it.
        """
        approval = self._store.get(self._root, name)
        if approval is None:  # pragma: no cover - only an approved spec reaches here
            return
        known = {source.path for source in approval.covered}
        added: list[CoveredSource] = []
        for raw in modules:
            relative = _relative(self._root, Path(raw))
            if relative is None or relative in known:
                continue
            known.add(relative)
            added.append(
                CoveredSource(
                    path=relative, digest=file_digest(self._root / relative), origin="imported"
                )
            )
        if not added:
            return
        covered = [*approval.covered, *sorted(added, key=lambda s: s.path)]
        updated = approval.model_copy(
            update={"covered": covered, "digest": composite_digest(name, covered)}
        )
        self._store.put(self._root, updated)
        log.info(
            "reconcile_spec.approval_extended",
            spec=name,
            added=[source.path for source in added],
            covered=len(covered),
        )

    def _record(
        self,
        name: str,
        outcome: SpecOutcome,
        trigger: str,
        ran_at: datetime,
        started: float,
        *,
        detail: str,
        validation_id: str | None = None,
        values: int = 0,
    ) -> SpecRunReport:
        report = SpecRunReport(
            name=name,
            outcome=outcome,
            validation_id=validation_id,
            trigger="watcher" if trigger == "watcher" else "manual",
            ran_at=ran_at,
            duration_ms=round((time.monotonic() - started) * 1000),
            values=values,
            detail=detail,
        )
        self._last_runs[name] = report
        log.info(
            "reconcile_spec.ran",
            spec=name,
            outcome=outcome,
            trigger=report.trigger,
            duration_ms=report.duration_ms,
            validation_id=validation_id,
        )
        state = self.state(name)
        if state is not None:
            self._publish(state)
        return report

    def _publish(self, state: SpecState) -> SpecState:
        self._bus.publish(SpecEvent(state=state))
        return state

    # ---- the loop -----------------------------------------------------------

    async def _consume(self) -> None:
        queue = self._queue
        if queue is None:  # pragma: no cover - start() always sets it
            return
        log.info("reconcile_spec.started", directory=str(self.directory))
        while True:
            event = await queue.get()
            if not isinstance(event, FileChangedEvent):
                continue
            try:
                self.note_file_change(event)
            except Exception:  # a dead task would stop the loop, silently
                log.exception("reconcile_spec.loop_failed", path=event.path)

    def note_file_change(self, event: FileChangedEvent) -> None:
        """One watcher event, routed. Synchronous and cheap — the work is
        deferred behind the debounce.

        **Keyed on the workbook**, never on "any save". ``.workbench/`` is not in
        ``IGNORED_DIRS``, so every file the app writes under it already publishes
        a change event; a trigger on any save would let an evidence write re-fire
        the gate that produced it, forever.
        """
        if event.is_dir:
            return
        spec_dir = SPEC_DIR.as_posix() + "/"
        if event.path.startswith(spec_dir) and event.path.endswith(SPEC_SUFFIX):
            # The spec itself changed: the row is refreshed (and an approved spec
            # goes `stale` the moment its bytes move) and **nothing runs**.
            state = self.state(Path(event.path).stem)
            if state is not None:
                self._publish(state)
            return
        names = self._specs_for_workbook(event.path)
        if not names:
            return
        self._schedule(event.path, names)

    def _specs_for_workbook(self, relative: str) -> list[str]:
        """Specs whose loop somebody armed, pointed at this path.

        Case-folded, because Windows paths are and a save reported as
        ``Models/Budget.xlsx`` must still re-run a spec that wrote
        ``models/budget.xlsx``.

        The filter is "**has a stored approval**", not "is currently approvable".
        A spec that has gone `stale` is still watched, and its run comes back
        `blocked` naming the file that changed — which is the point. A loop that
        simply went quiet when the code moved would be indistinguishable from a
        loop that broke, and the user would keep saving into a silence they had
        every reason to read as green.

        A spec nobody has *ever* approved is not watched at all: it would
        otherwise turn every save of any workbook into a refusal nobody asked for.
        """
        wanted = os.path.normcase(relative)
        names: list[str] = []
        for path in self._spec_files():
            loaded = self._load(path)
            if loaded.spec is None:
                continue
            if os.path.normcase(loaded.spec.workbook) != wanted:
                continue
            if self._store.get(self._root, loaded.name) is None:
                continue
            names.append(loaded.name)
        return names

    def _schedule(self, workbook: str, names: Sequence[str]) -> None:
        key = os.path.normcase(workbook)
        existing = self._pending.pop(key, None)
        if existing is not None:
            existing.cancel()
        task = asyncio.create_task(
            self._after_debounce(key, list(names)), name=f"reconcile-spec:{key}"
        )
        self._pending[key] = task
        self._running.add(task)
        # A *done callback* and not a `finally` inside the coroutine: a task
        # cancelled before its first step — which is every task a rapid burst of
        # saves supersedes — never executes a line of its own body, so a `finally`
        # there would leave it in this set forever.
        task.add_done_callback(self._running.discard)

    async def _after_debounce(self, key: str, names: Sequence[str]) -> None:
        current = asyncio.current_task()
        try:
            try:
                await asyncio.sleep(self._debounce_s)
            except asyncio.CancelledError:
                return
            # Past the debounce window and about to spawn. It leaves `_pending`
            # here — a newer save of the same workbook must not cancel a run that
            # already has a child — but it stays in `_running`, which is the map
            # shutdown reaches for. Dropping both here is the bug that let a
            # server stop while a subprocess was still running the analyst's code.
            self._unpend(key, current)
            for name in names:
                try:
                    await self.run(name, trigger="watcher")
                except Exception:  # one bad spec must not stop the next
                    log.exception("reconcile_spec.watcher_run_failed", spec=name)
        finally:
            self._unpend(key, current)

    def _unpend(self, key: str, task: asyncio.Task[None] | None) -> None:
        """Drop this key's debounce entry, but only when it is still *this* run.

        Identity-checked because a later save of the same workbook installs its
        own task under the same key: popping unconditionally would leave that
        newer, still-debouncing run with nothing able to supersede it.
        """
        if self._pending.get(key) is task:
            self._pending.pop(key, None)


# ---- compiling a spec into the gate's own vocabulary --------------------------


def compile_spec(
    spec: ReconcileSpecFile, values: SpecEntryResult
) -> tuple[ReconciliationSpec | None, list[str]]:
    """Turn a spec plus the values its callables produced into a
    :class:`ReconciliationSpec`.

    This is the whole architectural claim of PR-B: it produces ``ExpectedValue``
    and ``TimeExpectation`` records and hands them to the gate that already
    exists. **Nothing downstream of the check changes.**

    Returns ``(spec, problems)``. ``problems`` is one sentence per callable that
    produced nothing usable — never an exception that sinks the run, and never a
    silent omission that would leave a cell unproven while the badge stayed green.
    """
    answers = {value.call: value for value in values.values}
    expectations: list[ExpectedValue] = []
    per_cell: dict[str, Tolerance] = {}
    time_index: TimeIndexSpec | None = None
    problems: list[str] = []
    for check in spec.checks:
        answer = answers.get(check.call)
        if answer is None:
            problems.append(f"{check.call} produced no answer")
            continue
        if not answer.ok:
            problems.append(answer.error or f"{check.call} failed")
            continue
        if check.cell is not None:
            if answer.scalar is None:
                problems.append(
                    f"{check.call} answers cell {check.cell} but returned pairs, not a number"
                )
                continue
            expectations.append(
                ExpectedValue(
                    cell=check.cell,
                    expected=answer.scalar,
                    unit=check.unit,
                    cell_unit=check.value_unit,
                    label=check.label or check.call,
                )
            )
            if check.tolerance is not None:
                per_cell[check.cell] = check.tolerance
            continue
        parsed = parse_range(str(check.range))
        if parsed is None:
            problems.append(
                f"{check.call}: {check.range!r} is not a two-column A1 range like 'Hours!A2:B8761'"
            )
            continue
        if not answer.pairs:
            problems.append(f"{check.call} answers range {check.range} but returned no pairs")
            continue
        sheet, ts_column, value_column, start_row = parsed
        time_index = TimeIndexSpec(
            timestamp_column=ts_column,
            value_column=value_column,
            value_unit=check.value_unit,
            start_row=start_row,
            sheet=sheet,
            expectations=_time_expectations(check, answer),
        )
        if check.tolerance is not None:
            for expectation in time_index.expectations:
                per_cell[expectation.timestamp] = check.tolerance
    if not expectations and time_index is None:
        return None, problems or ["the spec produced no expected values"]
    return (
        ReconciliationSpec(
            workbook=spec.workbook,
            expectations=expectations,
            default_tolerance=spec.default_tolerance,
            per_cell_tolerance=per_cell,
            timezone=spec.timezone,
            time_index=time_index,
        ),
        problems,
    )


def _time_expectations(check: SpecCheck, answer: SpecValue) -> list[TimeExpectation]:
    """One expectation per returned pair, with ``fold`` from repetition.

    A repeated wall clock in the *code's* own output is the fall-back day's
    second 02:00 — the only way to address it, since an offset-bearing timestamp
    is refused on both sides. The check then asks the zone whether that hour is
    genuinely ambiguous, so a duplicated row in the code's output surfaces as a
    named fail rather than quietly matching the first occurrence.
    """
    seen: dict[str, int] = {}
    out: list[TimeExpectation] = []
    for stamp, value in answer.pairs:
        fold = seen.get(stamp, 0)
        seen[stamp] = fold + 1
        out.append(
            TimeExpectation(
                timestamp=stamp,
                expected=value,
                unit=check.unit,
                fold=fold,
                label=check.label or check.call,
            )
        )
    return out


def parse_range(text: str) -> tuple[str | None, str, str, int] | None:
    """``Hours!A2:B8761`` → ``("Hours", "A", "B", 2)``; ``None`` when it is not a
    two-column range.

    The **end row is advisory** and is deliberately dropped, which is a real
    limitation stated rather than hidden. ``WorkbookReader.column_pairs`` reads
    from ``start_row`` until it meets a row that is blank in both columns, so a
    range naming 8,761 stops where the data stops rather than where the spec
    guessed it would. Honouring the end row would mean changing the reader's
    contract, which is a different PR's file; a spec whose range is *shorter*
    than the data therefore reconciles the extra rows as unmatched workbook rows
    rather than ignoring them, which is the safer direction — nothing is quietly
    excluded from the proof.
    """
    match = _RANGE_RE.match(text.strip())
    if match is None:
        return None
    if int(match.group("r2")) < int(match.group("r1")):
        return None
    return (
        match.group("sheet"),
        match.group("c1").upper(),
        match.group("c2").upper(),
        int(match.group("r1")),
    )


# ---- sentences ----------------------------------------------------------------


def _changed_paths(before: Sequence[CoveredSource], after: Sequence[CoveredSource]) -> list[str]:
    """Which covered files no longer hash to what the approval recorded."""
    now = {source.path: source.digest for source in after}
    return [source.path for source in before if now.get(source.path) != source.digest]


def _stale_detail(changed: Sequence[str]) -> str:
    named = ", ".join(changed[:3]) if changed else "the covered set"
    more = f" (and {len(changed) - 3} more)" if len(changed) > 3 else ""
    return (
        f"Changed since approval: {named}{more}. Nothing runs until it is approved again — an "
        "approval covers this spec running exactly this code, and that code moved."
    )


def headline(result: ValidationResult) -> str:
    """The one sentence from a run worth putting on a chip's row.

    The *worst* evidence line's own detail, not the result's summary. A summary
    reads "high: 1 checks (1 fail)", which tells a person the colour they can
    already see; the evidence line reads "1 of 1 cells mismatch beyond tolerance.
    First: B2 in models/budget.xlsx", which tells them where to look. That is AXI
    shape 3 applied to a human: a round trip costs far more than the sentence
    that prevents it.
    """
    for wanted in ("fail", "warn", "skipped"):
        worst = next((item for item in result.evidence if item.outcome == wanted), None)
        if worst is not None:
            return worst.detail
    first = result.evidence[0].detail if result.evidence else None
    return first or result.summary


def _one_line(text: str, limit: int = 240) -> str:
    """The child's output as one bounded line for an evidence detail."""
    flattened = " ".join(text.split())
    if not flattened:
        return "(nothing)"
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _worse(outcome: SpecOutcome) -> SpecOutcome:
    """A run with a broken callable can never be a plain ``pass``.

    A green badge over a spec where one of the four callables raised is the
    silent green in miniature: the three that ran agreed, and the one that would
    have disagreed never spoke.
    """
    return "warn" if outcome == "pass" else outcome
