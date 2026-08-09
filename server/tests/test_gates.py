"""The toolchain gate — M6 staged review, PR 1.

Fake-first, and the split is deliberate. Everything about *judgement* — which
gates ran, what their outcomes mean, where the run is allowed to happen — is
driven through :class:`FakeGateRunner` and a scripted ``GitRunner``, so it is
deterministic on any machine and CI never runs a real ``pytest`` inside a slot.
Everything about *process handling* — the hard timeout, the bounded capture of a
chatty command, a command that cannot start, and whether the ``npm`` argv this
platform ships can be started at all — is driven through the **real**
:class:`SubprocessGateRunner`, because a fake cannot prove that a hung process is
killed, that 300 KB of output costs 8 KiB of memory, or that an executable
resolves.

The claim the whole milestone rests on has its own test: **a tree that moved
mid-run yields ``skipped``, never a pass.** Silent green is the enemy this
feature exists to kill, and a gate that reported one for a tree that no longer
exists would be the most expensive kind of lie this codebase can tell. That
claim is proven twice over. A scripted ``GitRunner`` drives its *shape* —
``HEAD`` moving, the dirty set growing, git refusing to answer — and
:class:`TestTheSameFilesDifferentBytes` drives its substance against **real git
and a real working tree**, with a writer editing the tree between the two
fingerprint reads. That half cannot be faked: a stub that returns whatever a
test handed it can only show the plumbing, and the fingerprint's job is to read
what is on the disk.
"""

import ast
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.gates import (
    MAX_GATE_LOG_BYTES,
    MAX_GATES_PER_RUN,
    MIN_GATE_LOG_BYTES,
    GateCommand,
    GateLog,
    GateSpec,
    SlotRef,
    head_bytes,
)
from workbench_server.models.validation import (
    EvidenceItem,
    EvidenceKind,
    ValidationSpec,
    ValidationSubject,
)
from workbench_server.services.event_bus import EventBus
from workbench_server.services.gates import (
    DEFAULT_GATE_IDS,
    GATE_CATALOG,
    SETTING_GATE_TIMEOUT,
    WORKSPACE_GATES_FILE,
    FakeGateRunner,
    GateRunner,
    SubprocessGateRunner,
    ToolchainGateCheck,
    _BoundedCapture,
    _Fingerprint,
    build_catalog,
    build_runner,
    configured_gate_ids,
    content_digest,
    launcher,
    workspace_config_refusal,
)
from workbench_server.services.validation import (
    ValidationContext,
    ValidationService,
    derive_risk,
)
from workbench_server.services.worktrees import GitResult, run_git

GATES_SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "workbench_server" / "services" / "gates.py"
).read_text(encoding="utf-8")
GATES_AST = ast.parse(GATES_SOURCE)

#: Every name and attribute the gate module actually *references*. The posture
#: assertions below read this rather than the source text, because the module
#: documents the things it refuses to do — a substring scan would fail on its own
#: prose, and would start passing the day the prose was deleted.
GATES_REFERENCED: set[str] = {
    node.id for node in ast.walk(GATES_AST) if isinstance(node, ast.Name)
} | {node.attr for node in ast.walk(GATES_AST) if isinstance(node, ast.Attribute)}


# --------------------------------------------------------------------------- doubles


class _Locator:
    """A ``SlotLocator`` that answers with one slot (or none)."""

    def __init__(self, ref: SlotRef | None) -> None:
        self._ref = ref
        self.asked: list[str] = []

    def slot_of(self, session_id: str) -> SlotRef | None:
        self.asked.append(session_id)
        return self._ref


class _ScriptedGit:
    """A ``GitRunner`` whose fingerprint reads a test drives.

    Each list yields its head until one value is left, which then repeats — so
    ``["a"]`` is a stable tree and ``["a", "b"]`` is one that moved between the
    before and after reads. ``dirt`` is how many tracked paths ``git diff HEAD
    --name-only -z`` should name; the untracked read answers empty.

    This double can drive *shape*: HEAD moving, the dirty set growing, git
    refusing to answer. It deliberately cannot drive the case where the same
    files hold different bytes — a stub that answers whatever it was told cannot
    show that the fingerprint reads the disk. :class:`TestTheSameFilesDifferentBytes`
    drives that against real git and a real working tree.
    """

    def __init__(self, heads: list[str], dirt: list[int]) -> None:
        self._heads = heads
        self._dirt = dirt

    async def __call__(self, cwd: Path, args: Sequence[str], timeout_s: float) -> GitResult:
        if "rev-parse" in args:
            head = self._heads.pop(0) if len(self._heads) > 1 else self._heads[0]
            if not head:
                return GitResult(128, "", "fatal: not a git repository")
            return GitResult(0, head, "")
        if "ls-files" in args:
            return GitResult(0, "", "")
        count = self._dirt.pop(0) if len(self._dirt) > 1 else self._dirt[0]
        return GitResult(0, "\0".join(f"file{index}.py" for index in range(count)), "")


class _EditingRunner:
    """A gate that writes into the tree while it "runs".

    Not a contrived double: **no lease is taken** (decision 2), so the session
    that owns the slot is free to keep saving into it for the whole of a
    ``pytest`` run. This is that session, compressed into the window between the
    two fingerprint reads.
    """

    def __init__(self, target: Path, text: str) -> None:
        self._target = target
        self._text = text

    async def run(self, command: GateCommand, cwd: Path, window: int) -> GateLog:
        self._target.write_text(self._text, encoding="utf-8")
        return GateLog(
            gate=command.id, argv=list(command.argv), exit_code=0, duration_ms=1, text="ok\n"
        )


def git(repo: Path, *args: str) -> str:
    """Synchronous git, for building a fixture. The check's own git is async.

    The same shape (and the same two suppressions) as ``test_worktrees.py``: the
    async rules ruff enforces are about production code, and a fixture that
    shells out is the entire point of this one.
    """
    done = subprocess.run(  # noqa: S603 - the arguments are this file's own literals
        ["git", *args],  # noqa: S607 - git is on PATH wherever this suite runs
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def make_repo(root: Path) -> Path:
    """A real repository with one commit and one tracked file.

    Real git rather than a scripted one, because the claim under test is about
    what is *on disk*: a double that returns the answers a test handed it can
    only prove the plumbing, and the plumbing was never the thing that was wrong.
    ``core.autocrlf=false`` per-repo — a developer machine may well have it on
    globally (``test_worktrees.py`` says the same), and the byte counts here
    would then depend on the machine.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@workbench.invalid")
    git(root, "config", "user.name", "Workbench Test")
    git(root, "config", "core.autocrlf", "false")
    (root / "dispatch.py").write_text("VERSION = 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "commit.gpgsign=false", "commit", "-m", "first")
    return root


def real_gate(slot: Path, runner: GateRunner) -> ToolchainGateCheck:
    """A check pointed at a real checkout, with **real git** as its runner."""
    return ToolchainGateCheck(
        _Locator(SlotRef(slot="slot-01", path=str(slot), base="c0ffee")),
        runner,
        catalog={"ruff": GATE_CATALOG[0]},
        default_gates=["ruff"],
        git=run_git,
    )


class _Stashing:
    """A check that stores one payload and names it — how a test mints a real
    ``payload_ref`` through the public path instead of poking the store."""

    id = "stash"

    def __init__(self, kind: EvidenceKind, payload: BaseModel) -> None:
        self.kind = kind
        self.payload = payload

    async def run(self, ctx: ValidationContext) -> list[EvidenceItem]:
        ref = ctx.store_payload(self.kind, self.payload)
        return [
            EvidenceItem(
                kind=self.kind, label="stashed", outcome="pass", detail="stashed", payload_ref=ref
            )
        ]


def context(
    tmp_path: Path,
    *,
    session_id: str = "wrk_1",
    kind: Literal["session_output", "file", "objective"] = "session_output",
    params: dict[str, object] | None = None,
) -> ValidationContext:
    return ValidationContext(
        subject=ValidationSubject(kind=kind, ref=session_id, label=session_id),
        root=tmp_path,
        params=params or {},
    )


def check(
    slot: SlotRef | None,
    *,
    runner: GateRunner | None = None,
    git: _ScriptedGit | None = None,
    gates: Sequence[str] | None = None,
) -> ToolchainGateCheck:
    return ToolchainGateCheck(
        _Locator(slot),
        runner or FakeGateRunner(),
        git=git or _ScriptedGit(["c0ffee"], [0]),
        default_gates=gates,
    )


def by_outcome(evidence: list[EvidenceItem], outcome: str) -> list[EvidenceItem]:
    return [item for item in evidence if item.outcome == outcome]


def python_gate(code: str, *, timeout_s: float = 30.0, gate_id: str = "probe") -> GateCommand:
    """A real, short program as a gate — the shape the catalog would own."""
    return GateCommand(
        id=gate_id,
        argv=(sys.executable, "-c", code),
        label=f"python -c ({gate_id})",
        timeout_s=timeout_s,
    )


# --------------------------------------------------------------------------- judgement


class TestVerdicts:
    async def test_a_failing_gate_is_a_fail_line_and_derives_high(self, tmp_path: Path) -> None:
        """One line per gate, not one grouped line — so a failing ``pytest`` is
        ``high`` even while ``ruff`` is clean, which is exactly what a reader
        needs to see side by side."""
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        evidence = await gate.run(context(tmp_path))

        assert [item.label for item in evidence] == [
            "ruff check .",
            "mypy --strict",
            "pytest -q",
            "npm run test (ui)",
        ]
        failing = by_outcome(evidence, "fail")
        assert [item.label for item in failing] == ["pytest -q"]
        assert "exit 1" in failing[0].detail
        assert failing[0].payload_ref is not None
        assert derive_risk(evidence) == "high"

    async def test_a_clean_run_is_four_pass_lines(self, tmp_path: Path) -> None:
        runner = FakeGateRunner({gate_id: (0, "ok\n") for gate_id in DEFAULT_GATE_IDS})
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"), runner=runner)
        evidence = await gate.run(context(tmp_path))

        assert len(by_outcome(evidence, "pass")) == 4
        assert derive_risk(evidence) == "pass"
        assert all(item.payload_ref is not None for item in evidence)

    async def test_an_unknown_gate_id_is_a_fail_line_never_a_silent_skip(
        self, tmp_path: Path
    ) -> None:
        """The frame's unregistered-check precedent: a name nobody serves is a
        stated failure, because a silent skip is a green nobody earned."""
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        evidence = await gate.run(context(tmp_path, params={"gates": ["ruff", "cargo-clippy"]}))

        assert [item.label for item in evidence] == ["cargo-clippy", "ruff check ."]
        unknown = evidence[0]
        assert unknown.outcome == "fail"
        assert "no gate 'cargo-clippy' is configured" in unknown.detail
        # And it names what *is* available rather than leaving the caller guessing.
        assert "ruff" in unknown.detail
        assert derive_risk(evidence) == "high"

    async def test_the_stored_payload_is_the_gates_own_log(self, tmp_path: Path) -> None:
        """Through the service, not around it: the check stashes into the frame's
        bounded store and the ref on its evidence redeems the log."""
        service = ValidationService(tmp_path, EventBus())
        service.register(
            check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"), gates=["pytest"])
        )
        result = await service.run(
            ValidationSpec(
                subject=ValidationSubject(kind="session_output", ref="wrk_1", label="wrk_1"),
                checks=["gates"],
            )
        )

        ref = result.evidence[0].payload_ref
        assert ref is not None
        stored = service.payload("gate", ref)
        assert isinstance(stored, GateLog)
        assert stored.gate == "pytest"
        assert stored.argv == ["uv", "run", "pytest", "-q"]
        assert "1 failed, 118 passed" in stored.text
        # The scripted failure carries a `path:line`, which is what lets the
        # `run_gates` "read this next" branch (AXI shape 3) be proven in fake mode.
        assert "server/tests/test_dispatch.py:118" in stored.text
        assert result.risk == "high"


# --------------------------------------------------------------------------- where it runs


class TestWhereItRuns:
    async def test_a_session_with_no_slot_is_one_skipped_line_naming_the_refusal(
        self, tmp_path: Path
    ) -> None:
        """Refusing is the feature. Falling back to the live workspace root would
        judge the user's unsaved changes and write caches into the folder they
        are editing — so the answer is a refusal that names the way to get a
        slot, never a result."""
        gate = check(None)
        evidence = await gate.run(context(tmp_path))

        assert len(evidence) == 1
        assert evidence[0].outcome == "skipped"
        assert "holds no worktree slot" in evidence[0].detail
        assert "Mission Control" in evidence[0].detail
        assert derive_risk(evidence) == "low"

    async def test_a_subject_that_is_not_a_session_is_refused(self, tmp_path: Path) -> None:
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        evidence = await gate.run(context(tmp_path, kind="file", session_id="book.xlsx"))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "session output" in evidence[0].detail

    async def test_a_checkout_that_is_gone_is_refused_not_run_somewhere_else(
        self, tmp_path: Path
    ) -> None:
        gate = check(SlotRef(slot="slot-09", path=str(tmp_path / "reaped"), base="c0ffee"))
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "is gone" in evidence[0].detail

    async def test_the_gate_asks_for_the_subject_sessions_own_slot(self, tmp_path: Path) -> None:
        """The slot is resolved from the subject, never supplied — there is no
        path, cwd or slot field anywhere in ``GateSpec`` to supply it with."""
        locator = _Locator(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        gate = ToolchainGateCheck(locator, FakeGateRunner(), git=_ScriptedGit(["c0ffee"], [0]))
        await gate.run(context(tmp_path, session_id="wrk_42"))
        assert locator.asked == ["wrk_42"]


# --------------------------------------------------------------------------- the fingerprint


class TestTheTreeThatMoved:
    """The regression test this milestone exists for."""

    async def test_a_head_that_moved_turns_a_passing_run_into_skipped(self, tmp_path: Path) -> None:
        """Every gate passed. The commit judged is not the commit that is there
        now, so the answer is ``skipped`` and the logs are discarded — a pass
        attributed to a tree that no longer exists is the silent green this whole
        feature refuses."""
        runner = FakeGateRunner({gate_id: (0, "ok\n") for gate_id in DEFAULT_GATE_IDS})
        gate = check(
            SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"),
            runner=runner,
            git=_ScriptedGit(["c0ffee", "deadbee"], [0]),
        )
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "the tree moved under the gate" in evidence[0].detail
        assert "c0ffee" in evidence[0].detail
        assert "deadbee" in evidence[0].detail
        assert "4 gate(s) ran" in evidence[0].detail
        # No pass line survives, and no payload is left claiming to describe it.
        assert by_outcome(evidence, "pass") == []
        assert evidence[0].payload_ref is None
        assert derive_risk(evidence) != "pass"

    async def test_a_working_tree_that_changed_is_also_skipped(self, tmp_path: Path) -> None:
        runner = FakeGateRunner({gate_id: (0, "ok\n") for gate_id in DEFAULT_GATE_IDS})
        gate = check(
            SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"),
            runner=runner,
            git=_ScriptedGit(["c0ffee"], [0, 3]),
        )
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "0 → 3 file(s)" in evidence[0].detail

    async def test_the_fingerprint_is_a_digest_of_bytes_not_a_headcount(self) -> None:
        """The property every test below leans on, asserted directly: two trees
        with the same *number* of dirty files and different contents do not
        fingerprint the same."""
        assert _Fingerprint("c0ffee", 2, "aaaa") == _Fingerprint("c0ffee", 2, "aaaa")
        assert _Fingerprint("c0ffee", 2, "aaaa") != _Fingerprint("c0ffee", 2, "bbbb")

    async def test_git_that_will_not_answer_is_a_refusal_not_a_clean_tree(
        self, tmp_path: Path
    ) -> None:
        """``None`` from the fingerprint is *not* zero. A checkout whose state
        cannot be vouched for is one no verdict may be attributed to
        (``services/worktrees.py``'s ``_dirty_count`` rule, one level up)."""
        gate = check(
            SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"),
            git=_ScriptedGit([""], [0]),
        )
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "git could not read" in evidence[0].detail


class TestTheSameFilesDifferentBytes:
    """Real git, a real working tree, and a real writer editing it mid-run.

    The case a dirty-file *count* is blind to, and the one the design invites:
    the gate takes no lease, so the session that owns the slot never stopped
    saving into it. ``HEAD`` has not moved and the same one file is dirty on both
    sides — a run that compared only those two numbers would call this a pass and
    attribute it to bytes that are no longer on disk.
    """

    async def test_a_tracked_file_rewritten_mid_run_is_caught(self, tmp_path: Path) -> None:
        """The file was **already dirty before the run started**, and the rewrite
        is the same length as what it replaced — so neither the file count nor
        the file's size moves, and on Windows the timestamp may not either
        (its granularity is the system clock tick, not what NTFS can store).
        Only the contents say what happened."""
        slot = make_repo(tmp_path / "slot")
        target = slot / "dispatch.py"
        target.write_text("VERSION = 2\n", encoding="utf-8")
        before = target.stat().st_size

        gate = real_gate(slot, _EditingRunner(target, "VERSION = 3\n"))
        evidence = await gate.run(context(tmp_path))

        assert target.stat().st_size == before  # the count *and* the size held still
        assert [item.outcome for item in evidence] == ["skipped"]
        assert "the tree moved under the gate" in evidence[0].detail
        assert "different bytes, same shape" in evidence[0].detail
        assert "1 gate(s) ran" in evidence[0].detail
        # Nothing survives that could be read as a verdict on this tree.
        assert by_outcome(evidence, "pass") == []
        assert evidence[0].payload_ref is None
        assert derive_risk(evidence) != "pass"

    async def test_an_untracked_file_rewritten_mid_run_is_caught_too(self, tmp_path: Path) -> None:
        """The half a diff against ``HEAD`` cannot see: a file the commit never
        had. An agent writing a *new* module and still saving it is the ordinary
        shape of this, not an exotic one."""
        slot = make_repo(tmp_path / "slot")
        target = slot / "new_module.py"
        target.write_text("draft = 1\n", encoding="utf-8")

        gate = real_gate(slot, _EditingRunner(target, "draft = 2\n"))
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "different bytes, same shape" in evidence[0].detail

    async def test_a_tree_nobody_touched_really_does_pass(self, tmp_path: Path) -> None:
        """The control, and it is not optional: a guard that reported ``skipped``
        for everything would pass every test above while making the feature
        useless. Same real repository, same real git, dirty file left alone."""
        slot = make_repo(tmp_path / "slot")
        (slot / "dispatch.py").write_text("VERSION = 2\n", encoding="utf-8")

        gate = real_gate(slot, FakeGateRunner({"ruff": (0, "All checks passed!\n")}))
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["pass"]
        assert derive_risk(evidence) == "pass"

    def test_the_digest_reads_the_disk_rather_than_the_directory_listing(
        self, tmp_path: Path
    ) -> None:
        """:func:`content_digest` on its own: same name, same size, different
        bytes is a different digest — and a file that has gone is a third answer
        again, never the same as one that never changed."""
        target = tmp_path / "dispatch.py"
        target.write_text("VERSION = 2\n", encoding="utf-8")
        stamp = target.stat().st_mtime_ns
        first = content_digest(tmp_path, ("dispatch.py",), 4_096)

        target.write_text("VERSION = 3\n", encoding="utf-8")
        os.utime(target, ns=(stamp, stamp))  # same name, same size, same clock
        assert content_digest(tmp_path, ("dispatch.py",), 4_096) != first

        target.write_text("VERSION = 2\n", encoding="utf-8")
        os.utime(target, ns=(stamp, stamp))
        assert content_digest(tmp_path, ("dispatch.py",), 4_096) == first

        target.unlink()
        assert content_digest(tmp_path, ("dispatch.py",), 4_096) != first

    def test_the_read_budget_bounds_the_work_without_losing_the_path(self, tmp_path: Path) -> None:
        """Past the budget the digest stops reading bytes and keeps size and
        timestamp — a coarser answer, never no answer, and never an unbounded
        read inside a request."""
        target = tmp_path / "huge.bin"
        target.write_bytes(b"a" * 4_096)
        stamp = target.stat().st_mtime_ns
        starved = content_digest(tmp_path, ("huge.bin",), 0)

        target.write_bytes(b"b" * 4_096)  # same size, new bytes…
        os.utime(target, ns=(stamp, stamp))  # …and the timestamp held still
        assert content_digest(tmp_path, ("huge.bin",), 0) == starved  # metadata alone
        assert content_digest(tmp_path, ("huge.bin",), 4_096) != starved  # bytes, once read


# --------------------------------------------------------------------------- the bounded log


class TestBoundedCapture:
    def test_head_and_tail_are_kept_and_the_middle_is_stated(self) -> None:
        capture = _BoundedCapture(1_000)
        capture.feed(b"H" * 400 + b"M" * 5_000 + b"T" * 400)
        text, truncated = capture.render()

        assert truncated is not None
        assert truncated.shown == 1_000
        assert truncated.total == 5_800
        assert "log_bytes" in truncated.detail
        # The head really is the head and the tail really is the tail.
        assert text.startswith("H" * 250)
        assert text.endswith("T" * 400)
        assert "4800 bytes withheld" in text

    def test_a_short_log_is_whole_and_says_nothing_about_truncation(self) -> None:
        capture = _BoundedCapture(MAX_GATE_LOG_BYTES)
        capture.feed(b"all good\n")
        text, truncated = capture.render()
        assert text == "all good\n"
        assert truncated is None

    def test_the_default_window_is_a_2_kib_head_and_a_6_kib_tail(self) -> None:
        assert MAX_GATE_LOG_BYTES == 8_192
        assert head_bytes(MAX_GATE_LOG_BYTES) == 2_048
        assert MAX_GATE_LOG_BYTES - head_bytes(MAX_GATE_LOG_BYTES) == 6_144

    def test_the_spec_clamps_its_window_rather_than_rejecting_it(self) -> None:
        assert GateSpec().window() == MAX_GATE_LOG_BYTES
        assert GateSpec(log_bytes=99_999).window() == MAX_GATE_LOG_BYTES
        assert GateSpec(log_bytes=1).window() == MIN_GATE_LOG_BYTES
        assert GateSpec(log_bytes=1_024).window() == 1_024


class TestRealSubprocess:
    """The half a fake cannot claim: real processes, really bounded."""

    async def test_a_chatty_gate_costs_the_window_not_the_output(self, tmp_path: Path) -> None:
        code = "import sys\nsys.stdout.write('START' + 'x' * 300000 + 'END\\n')\nsys.exit(3)\n"
        log = await SubprocessGateRunner().run(python_gate(code), tmp_path, MAX_GATE_LOG_BYTES)

        assert log.exit_code == 3
        assert log.truncated is not None
        assert log.truncated.total > 300_000
        assert log.truncated.shown == MAX_GATE_LOG_BYTES
        assert "log_bytes" in log.truncated.detail
        # Bounded *while reading*: what came back is the window plus the seam
        # marker, never the 300 KB the process actually printed.
        assert len(log.text.encode()) <= MAX_GATE_LOG_BYTES + 64
        assert log.text.startswith("START")
        assert log.text.rstrip().endswith("END")

    async def test_a_hanging_gate_is_killed_at_its_ceiling_and_named(self, tmp_path: Path) -> None:
        """A gate that hangs would hang the request that started it and, through
        it, the lifespan shutdown. So it is killed, and the evidence names the
        setting that would have let it finish."""
        command = python_gate("import time; time.sleep(60)", timeout_s=1.0, gate_id="pytest")
        gate = ToolchainGateCheck(
            _Locator(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee")),
            SubprocessGateRunner(),
            catalog={"pytest": command},
            default_gates=["pytest"],
            git=_ScriptedGit(["c0ffee"], [0]),
        )
        evidence = await gate.run(context(tmp_path))

        assert [item.outcome for item in evidence] == ["fail"]
        assert "no exit code" in evidence[0].detail
        assert SETTING_GATE_TIMEOUT in evidence[0].detail
        assert evidence[0].payload_ref is not None

    async def test_a_command_that_cannot_start_is_a_fail_that_says_so(self, tmp_path: Path) -> None:
        command = GateCommand(
            id="ruff",
            argv=("workbench-no-such-binary-2f9a",),
            label="ruff check .",
            timeout_s=5.0,
        )
        log = await SubprocessGateRunner().run(command, tmp_path, MAX_GATE_LOG_BYTES)
        assert log.exit_code is None
        assert "could not start" in log.text


# --------------------------------------------------------------------------- the catalog


class TestTheCatalogIsServerOwned:
    def test_no_field_in_the_spec_can_name_a_command(self) -> None:
        """The whole security story in one assertion: a caller selects by id, and
        there is nowhere in the request shape for an argv to arrive."""
        properties = GateSpec.model_json_schema()["properties"]
        assert set(properties) == {"gates", "log_bytes"}
        assert properties["gates"]["items"]["type"] == "string"

    def test_every_catalog_entry_is_an_exec_argv_with_no_shell_metacharacters(self) -> None:
        for command in GATE_CATALOG:
            assert command.argv, command.id
            assert command.timeout_s > 0
            assert command.pass_codes
            for part in command.argv:
                assert not set(part) & set("|&;<>$`\n"), command.id

    def test_the_module_never_reaches_for_a_shell(self) -> None:
        """Asserted over the parse tree, not the text: the module *documents*
        that it never uses ``shell=True``, so a substring scan would fail on its
        own docstring — and, worse, would pass on a module that only stopped
        talking about it."""
        for node in ast.walk(GATES_AST):
            if not isinstance(node, ast.Call):
                continue
            assert not any(keyword.arg == "shell" for keyword in node.keywords)
            called = ast.unparse(node.func)
            assert "create_subprocess_shell" not in called
            assert not called.endswith(("system", "popen", "Popen"))

    def test_the_gate_takes_no_lease(self) -> None:
        """A reader of a checkout somebody else borrowed. Acquiring a second slot
        would validate a different tree than the one the agent wrote."""
        for forbidden in ("acquire", "release", "renew", "AcquireWorktreeRequest"):
            assert forbidden not in GATES_REFERENCED, forbidden

    def test_the_check_never_approves(self) -> None:
        """Evidence, never authority. The human approval gate is the sole
        decider, and this asserts it rather than leaving it to review — the
        ``test_orchestrator.py`` "never auto-allow shell" precedent."""
        for forbidden in ("approve", "ValidationApproval", "ValidationResult"):
            assert forbidden not in GATES_REFERENCED, forbidden

    def test_the_operator_chooses_the_set_and_the_ceiling(self) -> None:
        assert configured_gate_ids("") == DEFAULT_GATE_IDS
        assert configured_gate_ids("  ,  ") == DEFAULT_GATE_IDS
        assert configured_gate_ids("ruff, pytest") == ("ruff", "pytest")

        catalog = build_catalog(42.0)
        assert {command.timeout_s for command in catalog.values()} == {42.0}
        assert set(catalog) == set(DEFAULT_GATE_IDS)
        # And the argv is untouched by the override — only the ceiling moves.
        assert catalog["ruff"].argv == build_catalog()["ruff"].argv

    def test_build_runner_honours_the_fake_flag(self) -> None:
        assert isinstance(build_runner(True), FakeGateRunner)
        assert isinstance(build_runner(False), SubprocessGateRunner)


class TestOneRunCannotAskForHoursOfWork:
    """``gates`` is the one field a caller fills, and every id in it buys a whole
    toolchain run. Unbounded, ``["pytest"] * 50`` is fifty serial ``pytest``
    invocations — hours — inside a single request, holding the session's slot for
    every one of them, reachable from the ``run_gates`` tool *and* from a plain
    ``POST /api/validation/run``."""

    def test_the_same_gate_named_fifty_times_is_one_gate(self) -> None:
        """Folded in the model, so both callers inherit it. A repeat can only
        mean one thing, and the second run would judge the identical tree."""
        assert GateSpec(gates=["pytest"] * 50).gates == ["pytest"]
        # Order survives the fold — the caller still gets what it asked for, once.
        assert GateSpec(gates=["mypy", "ruff", "mypy"]).gates == ["mypy", "ruff"]

    def test_more_distinct_ids_than_a_run_may_name_is_refused_in_words(self) -> None:
        too_many = [f"gate-{index}" for index in range(MAX_GATES_PER_RUN + 1)]
        with pytest.raises(ValidationError):
            GateSpec(gates=too_many)

    async def test_the_check_refuses_an_over_long_spec_rather_than_running_it(
        self, tmp_path: Path
    ) -> None:
        """And says which field and what the cap is — "1 error(s)" would leave a
        caller to guess between the two fields the spec has."""
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        evidence = await gate.run(
            context(
                tmp_path,
                params={"gates": [f"gate-{index}" for index in range(MAX_GATES_PER_RUN + 1)]},
            )
        )

        assert [item.outcome for item in evidence] == ["skipped"]
        assert "invalid gate spec: gates" in evidence[0].detail
        assert str(MAX_GATES_PER_RUN) in evidence[0].detail

    async def test_a_repeated_id_starts_one_process_not_fifty(self, tmp_path: Path) -> None:
        """The claim that actually matters, asserted on what *ran* rather than on
        what was parsed: one evidence line, one gate."""
        gate = check(SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"))
        evidence = await gate.run(context(tmp_path, params={"gates": ["pytest", "pytest"]}))

        assert [item.label for item in evidence] == ["pytest -q"]

    async def test_an_operators_repeated_default_is_folded_too(self, tmp_path: Path) -> None:
        """``WORKBENCH_GATES=ruff,ruff`` is the other way in, and it never passes
        through :class:`GateSpec` at all."""
        gate = check(
            SlotRef(slot="slot-01", path=str(tmp_path), base="c0ffee"),
            gates=["ruff", "ruff", "mypy"],
        )
        evidence = await gate.run(context(tmp_path))

        assert [item.label for item in evidence] == ["ruff check .", "mypy --strict"]

    def test_the_shipped_catalog_fits_inside_the_cap(self) -> None:
        """The cap is "no more than a catalog could hold". If a fifth gate ships,
        this is the line that says so before a caller finds out the hard way."""
        assert len(GATE_CATALOG) <= MAX_GATES_PER_RUN


class TestTheNpmGateCanActuallyStart:
    """Windows-first, and the one catalog entry where it is not cosmetic.

    Every gate is started with ``create_subprocess_exec`` and never a shell —
    which is the no-injection story — but a shell is also what normally performs
    the ``PATHEXT`` search. ``CreateProcess`` does not: handed an extension-less
    name it appends ``.exe`` and looks no further, and node ships npm as
    ``npm.cmd``. A bare ``"npm"`` argv is a gate that can never start on the
    platform this project targets, failing "could not start" on every run and
    marking the UI suite permanently red whatever that suite actually says.
    """

    NPM = next(command for command in GATE_CATALOG if command.id == "npm-test")

    def test_the_catalog_names_a_startable_executable_not_a_bare_npm(self) -> None:
        assert self.NPM.argv[1:] == ("--prefix", "ui", "run", "test")
        named = Path(self.NPM.argv[0])
        assert named.stem.lower() == "npm"
        if os.name == "nt":
            assert named.suffix.lower() in {".cmd", ".bat", ".exe"}, self.NPM.argv[0]

    def test_the_resolver_falls_back_to_a_name_this_platform_could_run(self) -> None:
        """Nothing on ``PATH`` still yields a plausible file, so the failure a
        developer reads is "npm is not installed" rather than a puzzle about
        extensions."""
        missing = launcher("workbench-no-such-binary-2f9a")
        assert missing == (
            "workbench-no-such-binary-2f9a.cmd"
            if os.name == "nt"
            else "workbench-no-such-binary-2f9a"
        )

    @pytest.mark.skipif(shutil.which("npm") is None, reason="node/npm is not installed here")
    async def test_the_resolved_npm_really_starts_through_the_real_runner(
        self, tmp_path: Path
    ) -> None:
        """The assertion a ``sys.executable`` probe cannot make. Every other
        real-subprocess test here runs Python, which resolves bare on every
        platform — precisely the property npm does not have, so the bug lived in
        the one argv no test ever started."""
        command = GateCommand(
            id="npm-test",
            argv=(self.NPM.argv[0], "--version"),
            label="npm --version",
            timeout_s=120.0,
        )
        entry = await SubprocessGateRunner().run(command, tmp_path, MAX_GATE_LOG_BYTES)

        assert entry.exit_code == 0, entry.text
        assert entry.text.strip()[:1].isdigit(), entry.text


class TestWorkspaceConfigIsRefused:
    def test_a_workspace_gates_json_is_refused_and_never_read(self, tmp_path: Path) -> None:
        """Opening a folder must never be enough to run that folder's commands.
        The file is not read — and the workspace is *told* it was ignored, because
        a config that is silently skipped is how an operator comes to believe a
        gate ran that never did."""
        config = tmp_path / WORKSPACE_GATES_FILE
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"gates": [{"id": "pwn", "argv": ["calc.exe"]}]}', encoding="utf-8")

        refusal = workspace_config_refusal(tmp_path)
        assert refusal is not None
        assert refusal.outcome == "skipped"
        assert "deliberately not read" in refusal.detail
        assert "WORKBENCH_GATES" in refusal.detail
        # Nothing from the file reached the catalog.
        assert "pwn" not in build_catalog()
        assert all(command.argv[0] != "calc.exe" for command in GATE_CATALOG)

    def test_no_file_is_no_refusal(self, tmp_path: Path) -> None:
        assert workspace_config_refusal(tmp_path) is None

    async def test_the_refusal_rides_along_with_a_real_run(self, tmp_path: Path) -> None:
        slot = tmp_path / "slot"
        (slot / WORKSPACE_GATES_FILE).parent.mkdir(parents=True)
        (slot / WORKSPACE_GATES_FILE).write_text("{}", encoding="utf-8")

        gate = check(SlotRef(slot="slot-01", path=str(slot), base="c0ffee"), gates=["ruff"])
        evidence = await gate.run(context(tmp_path))

        assert evidence[0].label == "workspace gate configuration"
        assert evidence[0].outcome == "skipped"
        assert [item.label for item in evidence[1:]] == ["ruff check ."]


# --------------------------------------------------------------------------- the payload route


def stash_log(app_state_validation: ValidationService, log: GateLog) -> None:
    app_state_validation.register(_Stashing("gate", log))


class TestThePayloadRoute:
    """The gap the #82 frame left, closed: a stored payload is redeemable, so
    ``payload_ref`` stops being a dead handle in the browser."""

    def test_it_round_trips_a_gate_log(self, settings: Settings) -> None:
        app = create_app(settings)
        log = GateLog(
            gate="pytest",
            argv=["uv", "run", "pytest", "-q"],
            exit_code=1,
            duration_ms=12_400,
            text="1 failed, 118 passed\n",
        )
        stash_log(app.state.validation, log)

        with TestClient(app) as client:
            run = client.post(
                "/api/validation/run",
                json={
                    "subject": {"kind": "file", "ref": "x", "label": "x"},
                    "checks": ["stash"],
                },
            ).json()
            ref = run["evidence"][0]["payload_ref"]

            found = client.get(f"/api/validation/payload/gate/{ref}")
            assert found.status_code == 200
            body = found.json()
            assert body["kind"] == "gate"
            assert body["ref"] == ref
            assert body["reconciliation"] is None
            assert body["gate_log"]["gate"] == "pytest"
            assert body["gate_log"]["exit_code"] == 1
            assert "118 passed" in body["gate_log"]["text"]

    def test_an_evicted_ref_is_a_404_not_a_guess(self, settings: Settings) -> None:
        app = create_app(settings)
        with TestClient(app) as client:
            gone = client.get("/api/validation/payload/gate/gate_neverminted")
            assert gone.status_code == 404
            assert "evicted" in gone.json()["detail"]

    def test_an_unknown_kind_is_rejected_by_the_schema(self, settings: Settings) -> None:
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/validation/payload/sql/x").status_code == 422

    def test_a_shape_this_build_cannot_render_is_refused_not_returned_empty(
        self, settings: Settings
    ) -> None:
        """An envelope with every field null is an emptiness a client can only
        read as either "nothing" or "broken" — AXI shape 2 forbids exactly that,
        so the router refuses instead. This is the shape a future check's payload
        would have against a client that predates its field."""
        app = create_app(settings)
        app.state.validation.register(
            _Stashing("artifact", ValidationSubject(kind="file", ref="a", label="a"))
        )
        with TestClient(app) as client:
            run = client.post(
                "/api/validation/run",
                json={
                    "subject": {"kind": "file", "ref": "x", "label": "x"},
                    "checks": ["stash"],
                },
            ).json()
            ref = run["evidence"][0]["payload_ref"]
            answer = client.get(f"/api/validation/payload/artifact/{ref}")
            assert answer.status_code == 404
            assert "render" in answer.json()["detail"]


# --------------------------------------------------------------------------- the wiring


class TestProductionWiring:
    def test_the_check_is_registered_and_runs_from_the_rest_api(self, settings: Settings) -> None:
        """End to end through the shipped wiring, in fake-gate mode: a session
        that holds no slot gets the refusal, which is the honest answer for a
        plain chat and the one a fallback would have replaced with a lie."""
        app = create_app(Settings(workspace_root=settings.workspace_root, gate_fake=True))
        with TestClient(app) as client:
            answer = client.post(
                "/api/validation/run",
                json={
                    "subject": {"kind": "session_output", "ref": "sess-x", "label": "sess-x"},
                    "checks": ["gates"],
                },
            )
            assert answer.status_code == 200
            body = answer.json()
            assert [item["outcome"] for item in body["evidence"]] == ["skipped"]
            assert "holds no worktree slot" in body["evidence"][0]["detail"]
            # A refusal is evidence, so the result is judged rather than blocked.
            assert body["risk"] == "low"

    def test_the_settings_knobs_exist_and_default_to_off(self) -> None:
        settings = Settings()
        assert settings.gates == ""
        assert settings.gate_timeout_s is None
        assert settings.gate_fake is False

    def test_the_orchestrator_locates_a_workers_slot_and_nothing_elses(
        self, settings: Settings
    ) -> None:
        """``slot_of`` is the injected seam, implemented where the roster lives.
        A session nobody spawned has no slot, which is what makes the refusal the
        default rather than the exception."""
        app = create_app(settings)
        assert app.state.orchestrator.slot_of("sess-nobody") is None
