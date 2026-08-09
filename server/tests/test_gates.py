"""The toolchain gate — M6 staged review, PR 1.

Fake-first, and the split is deliberate. Everything about *judgement* — which
gates ran, what their outcomes mean, where the run is allowed to happen, what
happens when the tree moves under it — is driven through :class:`FakeGateRunner`
and a scripted ``GitRunner``, so it is deterministic on any machine and CI never
runs a real ``pytest`` inside a slot. Everything about *process handling* — the
hard timeout, the bounded capture of a chatty command, a command that cannot
start — is driven through the **real** :class:`SubprocessGateRunner` against
short ``sys.executable`` programs, because a fake cannot prove that a hung
process is killed or that 300 KB of output costs 8 KiB of memory.

The claim the whole milestone rests on has its own test: **a tree that moved
mid-run yields ``skipped``, never a pass.** Silent green is the enemy this
feature exists to kill, and a gate that reported one for a tree that no longer
exists would be the most expensive kind of lie this codebase can tell.
"""

import ast
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.gates import (
    MAX_GATE_LOG_BYTES,
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
    build_catalog,
    build_runner,
    configured_gate_ids,
    workspace_config_refusal,
)
from workbench_server.services.validation import (
    ValidationContext,
    ValidationService,
    derive_risk,
)
from workbench_server.services.worktrees import GitResult

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
    """A ``GitRunner`` whose two fingerprint reads a test drives.

    Each list yields its head until one value is left, which then repeats — so
    ``["a"]`` is a stable tree and ``["a", "b"]`` is one that moved between the
    before and after reads.
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
        count = self._dirt.pop(0) if len(self._dirt) > 1 else self._dirt[0]
        return GitResult(0, "\n".join(f" M file{index}.py" for index in range(count)), "")


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
