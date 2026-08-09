"""Spec-from-code — the productivity loops' PR-B.

Fake-first, and the split is the same one ``test_gates.py`` draws. Everything
about *judgement* — what compiles into a ``ReconciliationSpec``, what may run,
what a mismatch does, what the watcher re-fires on — is driven through
:class:`FakeSpecRunner`, so it is deterministic on any machine and CI never
executes a line of workspace code. Everything about *process handling* — the
fixed argv, stdin delivery, the bounded capture, the hard timeout, a child that
cannot start, and whether a callable's own ``print`` can corrupt the parse — is
driven through the **real** :class:`SubprocessSpecRunner` against the real entry
module, because a fake cannot prove that a hung process is killed or that a
``print`` lands on the right pipe.

The claim the whole PR rests on has its own tests, and there are two of them:

* **No spec ever runs without a live approval of the code it names.** Six ways,
  because the trust boundary is the entire reason this feature is allowed to run
  workspace code at all. The load-bearing one is
  :func:`test_editing_the_callables_module_revokes_the_approval` — the test that
  would have caught a spec-bytes-only digest, which is a design that looks
  correct and authorises arbitrary code it never hashed.
* **Save the workbook and the chip flips, with nobody firing anything.** Driven
  end-to-end through the real watcher, the real bus and the real
  ``ReconciliationCheck`` in :class:`TestTheSaveToChipFlipLoop`.
"""

import asyncio
import json
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import openpyxl
import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.files import FileChangedEvent
from workbench_server.models.reconcile_spec import (
    MAX_SPEC_BYTES,
    MAX_SPEC_LOG_BYTES,
    SETTING_TIMEOUT,
    SPEC_DIR,
    CoveredSource,
    ReconcileSpecFile,
    SpecEntryRequest,
    SpecEntryResult,
    SpecValue,
)
from workbench_server.models.validation import ValidationResult, ValidationSpec
from workbench_server.services.event_bus import EventBus
from workbench_server.services.reconcile_spec import (
    FAKE_DEFAULT_VALUE,
    SPEC_ENTRY_ARGV,
    FakeSpecRunner,
    ReconcileSpecService,
    SpecApprovalStore,
    SpecConflict,
    SpecProblem,
    SubprocessSpecRunner,
    build_spec_runner,
    compile_spec,
    composite_digest,
    file_digest,
    module_outside_workspace,
    parse_range,
    resolve_module,
)
from workbench_server.services.reconciliation import ReconciliationCheck
from workbench_server.services.validation import ValidationService

# --------------------------------------------------------------------------- fixtures

APPROVER = "mathi"

#: The number the workbook fixture carries, and the number the fake runner
#: answers with. The whole loop is green until one of the two moves.
GOOD = FAKE_DEFAULT_VALUE

#: A callable body that returns the same number the workbook holds.
REPORTING_OK = "def annual_revenue():\n    return 1234.5\n"


def write_workbook(path: Path, value: float, *, cell: str = "B2") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    book = openpyxl.Workbook()
    sheet = book.active
    sheet[cell] = value
    book.save(path)


def write_spec(root: Path, name: str, body: str) -> Path:
    directory = root / SPEC_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


DEFAULT_SPEC = """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001
abs = 0.5

[[check]]
cell = "B2"
callable = "se3.reporting:annual_revenue"
unit = "NOK"
"""


def seed(root: Path, *, spec: str = DEFAULT_SPEC, value: float = GOOD) -> None:
    """A workspace with a workbook, the analyst's own code, and one spec."""
    write_workbook(root / "models" / "budget.xlsx", value)
    package = root / "se3"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "reporting.py").write_text(REPORTING_OK, encoding="utf-8")
    write_spec(root, "dispatch", spec)


def build_service(
    root: Path,
    store_dir: Path,
    runner: FakeSpecRunner | None = None,
    *,
    debounce_s: float = 0.0,
) -> tuple[ReconcileSpecService, FakeSpecRunner, EventBus]:
    """The real service over the real validation frame — only the runner is fake.

    ``ValidationService`` with the real ``ReconciliationCheck`` registered, so
    what these tests exercise is the gate that already exists being handed values
    rather than a second implementation of it.
    """
    bus = EventBus()
    validation = ValidationService(root, bus)
    validation.register(ReconciliationCheck())
    spec_runner = runner or FakeSpecRunner()
    service = ReconcileSpecService(
        root,
        bus,
        spec_runner,
        validation,
        SpecApprovalStore(store_dir),
        debounce_s=debounce_s,
    )
    return service, spec_runner, bus


def approve(service: ReconcileSpecService, name: str = "dispatch") -> None:
    state = service.state(name)
    assert state is not None
    service.approve(name, APPROVER, state.digest)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "ws"
    root.mkdir()
    seed(root)
    yield root


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "appdata"
    directory.mkdir()
    return directory


# --------------------------------------------------------------------------- the file


class TestTheSpecFile:
    def test_a_spec_is_a_toml_file_a_person_can_write(self, workspace: Path) -> None:
        document = ReconcileSpecFile.model_validate(
            {
                "workbook": "models/budget.xlsx",
                "timezone": "Europe/Oslo",
                "default_tolerance": {"rel": 0.001},
                "check": [
                    {
                        "cell": "Summary!D14",
                        "callable": "se3.reporting:annual_revenue",
                        "unit": "NOK",
                    },
                ],
            }
        )
        assert document.checks[0].call == "se3.reporting:annual_revenue"
        assert document.checks[0].address() == "Summary!D14"

    def test_a_check_names_exactly_one_address(self) -> None:
        for payload in (
            {"cell": "A1", "range": "A2:B3", "callable": "m:f"},
            {"callable": "m:f"},
        ):
            with pytest.raises(ValueError, match="exactly one"):
                ReconcileSpecFile.model_validate(
                    {
                        "workbook": "b.xlsx",
                        "default_tolerance": {"rel": 0.1},
                        "check": [payload],
                    }
                )

    def test_a_callable_must_be_module_colon_function(self) -> None:
        with pytest.raises(ValueError, match="module:function"):
            ReconcileSpecFile.model_validate(
                {
                    "workbook": "b.xlsx",
                    "default_tolerance": {"rel": 0.1},
                    "check": [{"cell": "A1", "callable": "se3.reporting"}],
                }
            )

    def test_a_range_check_needs_a_timezone(self) -> None:
        """The rule the DST aligner depends on, refused at load rather than at
        run: without a zone there is no way to tell a fall-back day's two 02:00s
        apart, which is the one thing the time index exists to do."""
        with pytest.raises(ValueError, match="timezone"):
            ReconcileSpecFile.model_validate(
                {
                    "workbook": "b.xlsx",
                    "default_tolerance": {"rel": 0.1},
                    "check": [{"range": "Hours!A2:B25", "callable": "m:f"}],
                }
            )

    def test_a_spec_that_is_not_toml_is_a_row_with_a_reason(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Not a spec that vanishes: a spec silently missing from the list is how
        somebody comes to believe a gate is running that never was."""
        write_spec(workspace, "broken", "this is not [ toml")
        service, _, _ = build_service(workspace, store_dir)
        broken = next(s for s in service.states().specs if s.name == "broken")
        assert broken.status == "invalid"
        assert "not readable TOML" in broken.detail

    def test_an_oversized_spec_is_refused_with_its_size(
        self, workspace: Path, store_dir: Path
    ) -> None:
        write_spec(workspace, "huge", "# " + "x" * (MAX_SPEC_BYTES + 1))
        service, _, _ = build_service(workspace, store_dir)
        huge = next(s for s in service.states().specs if s.name == "huge")
        assert huge.status == "invalid"
        assert str(MAX_SPEC_BYTES) in huge.detail

    def test_an_empty_folder_says_so_and_names_the_way_in(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        """AXI shape 2 applied to a person: an empty list states that it is empty
        rather than leaving blankness to interpret."""
        root = tmp_path / "bare"
        root.mkdir()
        service, _, _ = build_service(root, store_dir)
        states = service.states()
        assert states.specs == []
        assert "No reconciliation specs" in states.detail
        assert SPEC_DIR.as_posix() in states.detail


# --------------------------------------------------------------------------- compiling


class TestCompilingIntoTheGateThatAlreadyExists:
    """The whole architectural claim: a spec produces ``ExpectedValue`` and
    ``TimeExpectation`` records and hands them to the check that already exists.
    Nothing downstream of the check changes."""

    def test_a_cell_check_becomes_an_expected_value(self) -> None:
        spec = ReconcileSpecFile.model_validate(
            {
                "workbook": "models/budget.xlsx",
                "default_tolerance": {"rel": 0.001},
                "check": [
                    {"cell": "B2", "callable": "se3.reporting:annual_revenue", "unit": "NOK"}
                ],
            }
        )
        values = SpecEntryResult(
            ok=True,
            values=[SpecValue(call="se3.reporting:annual_revenue", ok=True, scalar=42.0)],
        )
        compiled, problems = compile_spec(spec, values)
        assert problems == []
        assert compiled is not None
        assert compiled.expectations[0].cell == "B2"
        assert compiled.expectations[0].expected == 42.0
        assert compiled.expectations[0].unit == "NOK"

    def test_the_declared_x1000_rides_into_the_evidence_as_a_named_conversion(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        """``value_unit`` is the workbook's unit and ``unit`` is the code's.

        The single most common silent bug in this domain is a value 1000x off
        because kWh was read as MWh. A spec that declares both makes the multiply
        a *named* line in the evidence — proven here by running the real check
        over a real workbook rather than by inspecting the compiled shape.
        """
        root = tmp_path / "units"
        root.mkdir()
        # The workbook holds kWh; the callable answers in MWh. 5,000 kWh is 5 MWh.
        write_workbook(root / "models" / "budget.xlsx", 5000.0)
        package = root / "se3"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "reporting.py").write_text("def energy():\n    return 5.0\n", encoding="utf-8")
        write_spec(
            root,
            "units",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.reporting:energy"
unit = "MWh"
value_unit = "kWh"
""",
        )
        service, _, _ = build_service(
            root,
            store_dir,
            FakeSpecRunner(
                {
                    "se3.reporting:energy": SpecValue(
                        call="se3.reporting:energy", ok=True, scalar=5.0
                    )
                }
            ),
        )
        approve(service, "units")
        report = asyncio.run(service.run("units"))
        assert report.outcome == "pass"

    def test_a_range_check_becomes_a_time_index_with_folds_from_repetition(self) -> None:
        """A repeated wall clock in the code's own output is the fall-back day's
        second 02:00 — the only way to address it, since an offset-bearing
        timestamp is refused on both sides of the comparison."""
        spec = ReconcileSpecFile.model_validate(
            {
                "workbook": "b.xlsx",
                "timezone": "Europe/Oslo",
                "default_tolerance": {"rel": 0.01},
                "check": [
                    {"range": "Hours!A2:B4", "callable": "se3.dispatch:hourly", "unit": "MWh"}
                ],
            }
        )
        values = SpecEntryResult(
            ok=True,
            values=[
                SpecValue(
                    call="se3.dispatch:hourly",
                    ok=True,
                    pairs=[
                        ("2024-10-27T01:00:00", 1.0),
                        ("2024-10-27T02:00:00", 2.0),
                        ("2024-10-27T02:00:00", 3.0),
                    ],
                )
            ],
        )
        compiled, problems = compile_spec(spec, values)
        assert problems == []
        assert compiled is not None and compiled.time_index is not None
        index = compiled.time_index
        assert (index.timestamp_column, index.value_column, index.start_row) == ("A", "B", 2)
        assert index.sheet == "Hours"
        assert [e.fold for e in index.expectations] == [0, 0, 1]

    def test_a_callable_that_failed_is_one_problem_line_not_a_sunk_run(self) -> None:
        spec = ReconcileSpecFile.model_validate(
            {
                "workbook": "b.xlsx",
                "default_tolerance": {"rel": 0.1},
                "check": [
                    {"cell": "A1", "callable": "m:good"},
                    {"cell": "A2", "callable": "m:bad"},
                ],
            }
        )
        values = SpecEntryResult(
            ok=True,
            values=[
                SpecValue(call="m:good", ok=True, scalar=1.0),
                SpecValue(call="m:bad", ok=False, error="m:bad raised — ValueError: nope"),
            ],
        )
        compiled, problems = compile_spec(spec, values)
        assert compiled is not None
        assert [e.cell for e in compiled.expectations] == ["A1"]
        assert problems == ["m:bad raised — ValueError: nope"]

    def test_a_spec_that_produced_nothing_compiles_to_nothing_and_says_why(self) -> None:
        spec = ReconcileSpecFile.model_validate(
            {
                "workbook": "b.xlsx",
                "default_tolerance": {"rel": 0.1},
                "check": [{"cell": "A1", "callable": "m:bad"}],
            }
        )
        compiled, problems = compile_spec(
            spec, SpecEntryResult(ok=True, values=[SpecValue(call="m:bad", ok=False, error="boom")])
        )
        assert compiled is None
        assert problems == ["boom"]

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Hours!A2:B8761", ("Hours", "A", "B", 2)),
            ("a2:b25", (None, "A", "B", 2)),
            ("A2:B2", (None, "A", "B", 2)),  # one row is a range too
            ("A9:B2", None),  # bottom-right above top-left is not a range
            ("A2", None),
            ("Hours!A:B", None),
        ],
    )
    def test_range_parsing(self, text: str, expected: tuple[Any, ...] | None) -> None:
        assert parse_range(text) == expected

    def test_more_than_one_range_check_is_refused_with_the_reason(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """One ``ReconciliationSpec`` carries one time index, so one spec file
        carries one range. Stated at load rather than discovered by the second
        range silently replacing the first."""
        write_spec(
            workspace,
            "two-ranges",
            """
workbook = "models/budget.xlsx"
timezone = "Europe/Oslo"

[default_tolerance]
rel = 0.1

[[check]]
range = "A2:B3"
callable = "se3.reporting:a"

[[check]]
range = "C2:D3"
callable = "se3.reporting:b"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        state = next(s for s in service.states().specs if s.name == "two-ranges")
        assert state.status == "invalid"
        assert "more than one `range` check" in state.detail


# --------------------------------------------------------------------------- trust


class TestTheTrustBoundary:
    """Six ways, because this is the whole reason PR-B is allowed to run
    workspace code — and because the posture it widens (the live workspace root,
    which ``ToolchainGateCheck`` refuses) is paid for by exactly this."""

    def test_1_a_never_approved_spec_runs_nothing_on_workspace_open(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Opening a workspace with specs in it runs nothing and prompts nothing.

        Asserted with a runner spy at **zero calls** rather than by reading a
        status: the status is what the panel renders, and the claim is about what
        the machine did.
        """
        service, runner, _ = build_service(workspace, store_dir)
        states = service.states()
        assert [s.status for s in states.specs] == ["unapproved"]
        report = asyncio.run(service.run("dispatch"))
        assert report.outcome == "blocked"
        assert runner.calls == []
        assert "Nothing was run" in report.detail

    def test_2_editing_the_spec_by_one_byte_revokes_it(
        self, workspace: Path, store_dir: Path
    ) -> None:
        service, runner, _ = build_service(workspace, store_dir)
        approve(service)
        assert service.state("dispatch") is not None
        spec_path = workspace / SPEC_DIR / "dispatch.toml"
        spec_path.write_text(spec_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        state = service.state("dispatch")
        assert state is not None and state.status == "stale"
        assert "dispatch.toml" in state.detail
        report = asyncio.run(service.run("dispatch"))
        assert report.outcome == "blocked"
        assert runner.calls == []

    def test_3_editing_the_callables_module_revokes_it_just_as_hard(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """**The test that would have caught a spec-bytes-only digest.**

        Approve, rewrite ``annual_revenue``'s body, save the workbook, and assert
        the watcher path produced ``blocked`` with zero runner calls — not a
        re-run. The spec file is untouched throughout, which is exactly why
        hashing it alone would have authorised whatever that function now
        contains, unattended, forever.
        """
        service, runner, _ = build_service(workspace, store_dir)
        approve(service)
        (workspace / "se3" / "reporting.py").write_text(
            "def annual_revenue():\n    return 9999.0\n", encoding="utf-8"
        )
        state = service.state("dispatch")
        assert state is not None and state.status == "stale"
        assert "se3/reporting.py" in state.detail

        # …and the *watcher* path, which is the one that runs unattended.
        async def saved() -> None:
            service.note_file_change(_change("models/budget.xlsx"))
            await _settle(service)

        asyncio.run(saved())
        assert runner.calls == []
        last = service.state("dispatch")
        assert last is not None and last.last_run is not None
        # `blocked`, not silence: a loop that simply went quiet when the code
        # moved is indistinguishable from a loop that broke.
        assert last.last_run.outcome == "blocked"
        assert last.last_run.trigger == "watcher"
        assert "se3/reporting.py" in last.last_run.detail

    def test_4_a_callable_pointed_at_a_different_module_is_a_mismatch(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Not a fresh implicit approval. The spec's own bytes are covered, so
        re-pointing ``callable`` moves the digest — and the *resolution* is
        recorded too, so a module that becomes a package is caught as well."""
        service, runner, _ = build_service(workspace, store_dir)
        approve(service)
        (workspace / "se3" / "other.py").write_text(REPORTING_OK, encoding="utf-8")
        spec_path = workspace / SPEC_DIR / "dispatch.toml"
        spec_path.write_text(
            spec_path.read_text(encoding="utf-8").replace("se3.reporting", "se3.other"),
            encoding="utf-8",
        )
        state = service.state("dispatch")
        assert state is not None and state.status == "stale"
        report = asyncio.run(service.run("dispatch"))
        assert report.outcome == "blocked"
        assert runner.calls == []

    def test_5_a_helper_that_participated_in_run_1_revokes_before_run_2(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """The ``sys.modules`` closure, asserted through ``covered[]``.

        ``se3/reporting.py`` can keep its bytes and pull its arithmetic from
        ``se3/_helpers.py``. Static import analysis would be a guess, so the child
        reports what it *actually* imported and the approval grows to cover it.
        Both halves of the honest consequence are here: run 1 is covered only to
        the entry points, and from run 2 on the helper is covered too.
        """
        helper = workspace / "se3" / "_helpers.py"
        helper.write_text("RATE = 1.0\n", encoding="utf-8")
        runner = FakeSpecRunner(modules=[str(helper)])
        service, _, _ = build_service(workspace, store_dir, runner)
        approve(service)

        before = service.state("dispatch")
        assert before is not None and before.approval is not None
        assert "se3/_helpers.py" not in [c.path for c in before.approval.covered]

        first = asyncio.run(service.run("dispatch"))
        assert first.outcome == "pass"
        after = service.state("dispatch")
        assert after is not None and after.approval is not None
        covered = {c.path: c.origin for c in after.approval.covered}
        assert covered["se3/_helpers.py"] == "imported"
        assert after.status == "approved"

        helper.write_text("RATE = 2.0\n", encoding="utf-8")
        stale = service.state("dispatch")
        assert stale is not None and stale.status == "stale"
        assert "se3/_helpers.py" in stale.detail
        second = asyncio.run(service.run("dispatch"))
        assert second.outcome == "blocked"
        assert len(runner.calls) == 1  # run 2 never spawned

    def test_6_a_broken_callable_is_evidence_not_an_exception(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """A callable that raises, hangs or prints 50 MB becomes one line with
        the setting that raises the ceiling — never something that sinks the run."""
        write_spec(
            workspace,
            "broken-calls",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.reporting:annual_revenue"

[[check]]
cell = "B3"
callable = "se3.reporting:raises_here"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        approve(service, "broken-calls")
        report = asyncio.run(service.run("broken-calls"))
        # The one good cell still reconciled; the broken one is named, and the
        # run can never be a plain `pass` with a callable that never spoke.
        assert report.outcome == "warn"
        assert "raises_here" in report.detail

    def test_a_failing_run_names_the_cell_rather_than_only_the_colour(
        self, tmp_path: Path, store_dir: Path
    ) -> None:
        """AXI shape 3, applied to a human. "high: 1 checks (1 fail)" tells a
        reader the colour they can already see; the evidence line tells them
        which cell to open."""
        root = tmp_path / "wrong"
        root.mkdir()
        seed(root, value=9999.0)
        service, _, _ = build_service(root, store_dir)
        approve(service)
        report = asyncio.run(service.run("dispatch"))
        assert report.outcome == "fail"
        assert "B2" in report.detail
        assert "models/budget.xlsx" in report.detail
        assert "risk high" in report.detail

    def test_a_hanging_callable_names_the_setting_that_raises_the_ceiling(
        self, workspace: Path, store_dir: Path
    ) -> None:
        write_spec(
            workspace,
            "slow",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.reporting:hangs_forever"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        approve(service, "slow")
        report = asyncio.run(service.run("slow"))
        assert report.outcome == "fail"
        assert SETTING_TIMEOUT in report.detail

    def test_a_chatty_callable_costs_the_window_not_the_memory(
        self, workspace: Path, store_dir: Path
    ) -> None:
        write_spec(
            workspace,
            "loud",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.reporting:chatty_annual_revenue"
""",
        )
        runner = FakeSpecRunner()
        service, _, _ = build_service(workspace, store_dir, runner)
        approve(service, "loud")
        output = asyncio.run(
            runner.run(
                SpecEntryRequest(callables=["se3.reporting:chatty_annual_revenue"]),
                workspace,
                5.0,
                MAX_SPEC_LOG_BYTES,
            )
        )
        assert output.truncated is not None
        assert output.truncated.total > MAX_SPEC_LOG_BYTES
        assert len(output.log.encode()) <= MAX_SPEC_LOG_BYTES + 200  # + the seam marker

    def test_approving_a_digest_that_moved_is_refused(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Approving a spec whose bytes moved under the dialog would approve
        something nobody read."""
        service, _, _ = build_service(workspace, store_dir)
        state = service.state("dispatch")
        assert state is not None
        (workspace / "se3" / "reporting.py").write_text("def annual_revenue():\n    return 0\n")
        with pytest.raises(SpecConflict, match="changed since it was shown"):
            service.approve("dispatch", APPROVER, state.digest)

    def test_revoking_puts_it_back_to_unapproved(self, workspace: Path, store_dir: Path) -> None:
        service, runner, _ = build_service(workspace, store_dir)
        approve(service)
        state = service.revoke("dispatch")
        assert state is not None and state.status == "unapproved"
        assert asyncio.run(service.run("dispatch")).outcome == "blocked"
        assert runner.calls == []

    def test_the_approval_is_not_written_into_the_folder_it_authorises(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """``RecentsStore``'s reason, sharpened by what this one authorises: a
        trust record inside the folder it authorises is one an attacker can write."""
        service, _, _ = build_service(workspace, store_dir)
        approve(service)
        assert (store_dir / "reconcile-approvals.json").is_file()
        assert list((workspace / ".workbench").rglob("*approval*")) == []
        assert list((workspace / ".workbench").rglob("*.json")) == []

    def test_an_unreadable_approvals_document_fails_closed(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Nothing approved, plus a sentence saying why — and *closed* is the
        safe direction: an unreadable trust record must mean nothing runs."""
        (store_dir / "reconcile-approvals.json").write_text("{not json", encoding="utf-8")
        service, runner, _ = build_service(workspace, store_dir)
        state = service.state("dispatch")
        assert state is not None and state.status == "unapproved"
        assert asyncio.run(service.run("dispatch")).outcome == "blocked"
        assert runner.calls == []

    def test_7_a_callable_that_names_an_installed_package_is_refused_not_approved(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """The hole the composite digest could not see, because it never looked.

        ``resolve_module`` fabricates an in-tree candidate for **any** dotted
        name, so a spec naming a distribution that is actually installed recorded
        ``<name>.py`` at ``absent`` — a path nobody will ever create, whose digest
        therefore never moves. The approval would stand forever while
        ``spec_entry`` imported the real thing off ``sys.path``: a dependency
        bump, or a compromise of that package, running unattended on every save
        with no staleness and no re-prompt. ``json`` stands in for the analyst's
        installed dependency here — it is a package Python answers from the
        stdlib on every machine this suite runs on.
        """
        write_spec(
            workspace,
            "shadowed",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "json.decoder:JSONDecoder"
""",
        )
        service, runner, _ = build_service(workspace, store_dir)
        state = service.state("shadowed")
        assert state is not None and state.status == "invalid"
        assert "not this workspace's code" in state.detail
        assert "json" in state.detail

        # …and it cannot be talked into an approval, so the absent digest never
        # becomes a standing authorisation.
        with pytest.raises(SpecProblem, match="not this workspace's code"):
            service.approve("shadowed", APPROVER, state.digest)
        assert asyncio.run(service.run("shadowed")).outcome == "blocked"
        assert runner.calls == []

    def test_a_module_not_written_yet_is_still_allowed(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """The other side of the same coin, and the distinction the refusal above
        turns on: ``se3.not_yet`` addresses a place *inside* this workspace, so it
        is a module nobody has written rather than a name the import system
        already answers. ``absent`` there is a promise that moves the day the file
        appears (:class:`TestTheCompositeDigest`), which is a real guard."""
        write_spec(
            workspace,
            "future",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.not_yet:value"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        state = service.state("future")
        assert state is not None and state.status == "unapproved"

    def test_a_module_name_that_is_not_one_is_invalid_rather_than_unapprovable_forever(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """A name that cannot address a file under the root has the same shape of
        defect — a covered line whose digest can never move — so it is a stated
        refusal rather than a row that looks armable."""
        write_spec(
            workspace,
            "notaname",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "../escape:value"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        state = service.state("notaname")
        assert state is not None and state.status == "invalid"
        assert "not a module name" in state.detail

    def test_an_approval_survives_a_new_store_object(
        self, workspace: Path, store_dir: Path
    ) -> None:
        service, _, _ = build_service(workspace, store_dir)
        approve(service)
        again, runner, _ = build_service(workspace, store_dir)
        state = again.state("dispatch")
        assert state is not None and state.status == "approved"
        assert state.approval is not None and state.approval.approver == APPROVER
        assert asyncio.run(again.run("dispatch")).outcome == "pass"
        assert len(runner.calls) == 1


# --------------------------------------------------------------------------- digests


class TestTheCompositeDigest:
    def test_a_missing_module_is_covered_as_absent_not_omitted(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """Approve while a module is missing, then create it: the digest moves.

        Recording "absent" rather than dropping the line is what stops "approve
        before writing the code" being a way to pre-authorise it.
        """
        write_spec(
            workspace,
            "future",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "se3.not_yet:value"
""",
        )
        service, _, _ = build_service(workspace, store_dir)
        approve(service, "future")
        state = service.state("future")
        assert state is not None and state.approval is not None
        assert ("se3/not_yet.py", "absent") in [(c.path, c.digest) for c in state.approval.covered]
        (workspace / "se3" / "not_yet.py").write_text("def value():\n    return 1\n")
        after = service.state("future")
        assert after is not None and after.status == "stale"

    def test_the_covered_list_reaches_the_panel_before_the_click_not_after(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """The receipt has to arrive with the question, not with the answer.

        ``SpecApproval.covered`` is ``None`` until a spec *has* been approved, so
        it can only ever be read by somebody who already trusted it. The state a
        user is looking at when they decide carries ``pending_covered`` — the
        exact list Approve would stand for — which is the difference between an
        informed one-time trust decision and a rubber stamp.
        """
        service, _, _ = build_service(workspace, store_dir)
        before = service.state("dispatch")
        assert before is not None and before.status == "unapproved"
        assert before.approval is None
        assert [(c.path, c.origin) for c in before.pending_covered] == [
            (".workbench/reconcile/dispatch.toml", "spec"),
            ("se3/reporting.py", "callable"),
        ]

        approve(service)
        after = service.state("dispatch")
        assert after is not None and after.approval is not None
        # Once the approval carries the same receipt, the row stops paying for it
        # twice on every fan-out.
        assert after.pending_covered == []
        assert [c.path for c in after.approval.covered] == [c.path for c in before.pending_covered]

        # …and a stale row carries both: what was trusted, and what "Approve
        # again" would trust. The difference between them *is* the question.
        (workspace / "se3" / "reporting.py").write_text(
            "def annual_revenue():\n    return 9999.0\n", encoding="utf-8"
        )
        stale = service.state("dispatch")
        assert stale is not None and stale.status == "stale"
        assert stale.approval is not None
        assert [c.path for c in stale.pending_covered] == [c.path for c in stale.approval.covered]
        assert [c.digest for c in stale.pending_covered] != [
            c.digest for c in stale.approval.covered
        ]

    def test_the_digest_folds_order_path_and_bytes(self, tmp_path: Path) -> None:
        one = CoveredSource(path="a.py", digest="aa", origin="callable")
        two = CoveredSource(path="b.py", digest="bb", origin="callable")
        assert composite_digest("x", [one, two]) != composite_digest("x", [two, one])
        assert composite_digest("x", [one]) != composite_digest("y", [one])
        moved = one.model_copy(update={"digest": "cc"})
        assert composite_digest("x", [one]) != composite_digest("x", [moved])

    def test_file_digest_of_a_missing_file_is_absent(self, tmp_path: Path) -> None:
        assert file_digest(tmp_path / "nope.py") == "absent"

    @pytest.mark.parametrize(
        ("module", "expected"),
        [
            ("se3.reporting", "se3/reporting.py"),
            ("se3", "se3/__init__.py"),
            ("not.here", "not/here.py"),
        ],
    )
    def test_module_resolution(self, workspace: Path, module: str, expected: str) -> None:
        resolved = resolve_module(workspace, module)
        assert resolved is not None
        assert resolved.relative_to(workspace).as_posix() == expected

    @pytest.mark.parametrize("module", ["../escape", "se3/reporting", "", "se3..reporting"])
    def test_a_module_name_that_is_not_one_is_refused(self, workspace: Path, module: str) -> None:
        """Refused rather than resolved to something plausible — a name that can
        address a file outside the root is not a module name."""
        assert resolve_module(workspace, module) is None

    @pytest.mark.parametrize("module", ["se3.reporting", "se3", "se3.not_yet", "brand_new"])
    def test_a_name_that_is_this_workspace_is_not_called_shadowed(
        self, workspace: Path, module: str
    ) -> None:
        """Written, not written yet, and a top-level name nothing answers to —
        all three are the workspace's own, and the ``absent`` digest recorded for
        the unwritten ones is a promise that moves the day somebody writes them."""
        assert module_outside_workspace(workspace, module) is None

    @pytest.mark.parametrize("module", ["json", "json.decoder", "sys", "unittest.mock"])
    def test_a_name_the_import_system_already_answers_is_named_as_shadowed(
        self, workspace: Path, module: str
    ) -> None:
        """The stdlib stands in for any installed distribution: the digest would
        be recorded against an in-tree path that will never exist, while the code
        that actually ran came from somewhere the approval never looked."""
        reason = module_outside_workspace(workspace, module)
        assert reason is not None
        assert "not this workspace's code" in reason


# --------------------------------------------------------------------------- the loop


def _change(path: str, change: Literal["added", "modified", "deleted"] = "modified") -> Any:
    return FileChangedEvent(path=path, change=change, hash=None, is_dir=False)


async def _settle(service: ReconcileSpecService) -> None:
    """Let every scheduled run finish.

    Awaits the *tasks* rather than sleeping on a wall clock: the fixtures use
    ``debounce_s=0``, so what has to be waited for is the run behind the debounce
    (which really does hit a thread and a subprocess seam), not a timer.

    ``_running`` and not ``_pending``: a run leaves the debounce map the moment
    it is past its window, and the set is the only handle on one that is actually
    executing — which is the same reason ``stop()`` reaches for it.
    """
    while tasks := list(service._running):  # the scheduled set is the point
        await asyncio.gather(*tasks, return_exceptions=True)
    await asyncio.sleep(0)


class TestTheDebounceIsKeyedOnTheWorkbook:
    """The plan's correction, and it is not a detail: ``.workbench/`` is **not**
    in ``IGNORED_DIRS``, so every file the app writes under it already publishes
    a change event. A trigger on "any save" would let an evidence write re-fire
    the gate that produced it, forever."""

    def test_a_workbench_write_does_not_re_fire_the_loop(
        self, workspace: Path, store_dir: Path
    ) -> None:
        async def scenario() -> None:
            service, runner, _ = build_service(workspace, store_dir)
            approve(service)
            service.note_file_change(_change(".workbench/validation/results.jsonl", "added"))
            service.note_file_change(_change("README.md"))
            service.note_file_change(_change("se3/reporting.py"))
            await _settle(service)
            assert runner.calls == []

        asyncio.run(scenario())

    def test_a_workbook_save_fires_exactly_one_run(self, workspace: Path, store_dir: Path) -> None:
        async def scenario() -> None:
            service, runner, _ = build_service(workspace, store_dir)
            approve(service)
            # Excel's write-temp-then-rename dance is several events; the
            # debounce is what makes it one run.
            for _ in range(3):
                service.note_file_change(_change("models/budget.xlsx"))
            await _settle(service)
            assert len(runner.calls) == 1

        asyncio.run(scenario())

    def test_a_spec_edit_refreshes_the_row_and_runs_nothing(
        self, workspace: Path, store_dir: Path
    ) -> None:
        async def scenario() -> None:
            service, runner, bus = build_service(workspace, store_dir)
            approve(service)
            queue = bus.subscribe()
            service.note_file_change(_change(f"{SPEC_DIR.as_posix()}/dispatch.toml"))
            await _settle(service)
            assert runner.calls == []
            event = queue.get_nowait()
            assert getattr(event, "type", None) == "reconcile_spec"

        asyncio.run(scenario())

    def test_an_unapproved_specs_workbook_is_not_watched(
        self, workspace: Path, store_dir: Path
    ) -> None:
        async def scenario() -> None:
            service, runner, _ = build_service(workspace, store_dir)
            service.note_file_change(_change("models/budget.xlsx"))
            await _settle(service)
            assert runner.calls == []

        asyncio.run(scenario())

    def test_a_workspace_switch_forgets_the_runs_and_keeps_the_approvals(
        self, workspace: Path, store_dir: Path, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        seed(other)
        service, _, _ = build_service(workspace, store_dir)
        approve(service)
        assert asyncio.run(service.run("dispatch")).outcome == "pass"
        service.set_workspace_root(other)
        moved = service.state("dispatch")
        assert moved is not None
        assert moved.status == "unapproved"  # a different project's decisions
        assert moved.last_run is None
        service.set_workspace_root(workspace)
        back = service.state("dispatch")
        assert back is not None and back.status == "approved"


# --------------------------------------------------------------------------- fake mode


class TestTheFakeRunner:
    def test_the_default_script_is_name_driven(self, workspace: Path, store_dir: Path) -> None:
        """The ``FakeDocumentBridge`` precedent: the *name* of the thing scripts
        the behaviour, so a fixture is a file rather than a knob."""

        async def scenario() -> None:
            runner = FakeSpecRunner()
            output = await runner.run(
                SpecEntryRequest(callables=["m:plain", "m:tariff_v50", "m:raises_now"]),
                workspace,
                5.0,
                MAX_SPEC_LOG_BYTES,
            )
            assert output.result is not None
            values = {v.call: v for v in output.result.values}
            assert values["m:plain"].scalar == FAKE_DEFAULT_VALUE
            assert values["m:tariff_v50"].scalar == 50.0
            assert values["m:raises_now"].ok is False

        asyncio.run(scenario())

    def test_build_spec_runner_selects_the_fake(self) -> None:
        assert isinstance(build_spec_runner(fake=True), FakeSpecRunner)
        assert isinstance(build_spec_runner(fake=False), SubprocessSpecRunner)


# --------------------------------------------------------------------------- the argv


class TestTheArgvIsFixedAndServerOwned:
    def test_the_shipped_argv_is_the_planned_one(self) -> None:
        """``models/gates.py``'s rule, held one module further out: no field
        anywhere in a spec reaches an argv, a cwd or a path."""
        assert SPEC_ENTRY_ARGV == ("uv", "run", "python", "-m", "workbench_server.spec_entry")
        assert SubprocessSpecRunner().argv == SPEC_ENTRY_ARGV

    def test_nothing_a_spec_says_can_reach_the_argv(self) -> None:
        """The request the runner is handed carries callables and a cap, and
        nothing else. Asserted on the *shape* rather than on a string, so a field
        added later fails this test instead of quietly widening the surface."""
        assert set(SpecEntryRequest.model_fields) == {"callables", "max_pairs"}


# --------------------------------------------------------------------------- real process


#: The real entry module through the *current* interpreter rather than through
#: ``uv``. Still server-owned and still assembled from nothing in a request —
#: this is the ``test_gates.py`` split: the fake proves judgement, the real
#: process proves process handling, and paying ``uv``'s resolution per assertion
#: would buy nothing but seconds.
LOCAL_ARGV = (sys.executable, "-m", "workbench_server.spec_entry")


def _alive(proc: asyncio.subprocess.Process) -> bool:
    """Is this child still running?

    A function rather than an inline ``proc.returncode is None`` because the
    inline form is the *same expression* before and after the kill, and narrowing
    it once pins it: ``assert ... is None`` teaches mypy the attribute is ``None``
    and nothing in between un-teaches it, so the later "it died" assertion reads
    as unreachable. Asking through a call keeps each answer its own.
    """
    return proc.returncode is None


class TestTheRealSubprocess:
    """What a fake cannot prove: that a hung process is killed, that a callable's
    ``print`` lands on the right pipe, and that ``sys.modules`` really answers."""

    @staticmethod
    def _workspace(tmp_path: Path, body: str) -> Path:
        root = tmp_path / "real"
        root.mkdir()
        (root / "mymod.py").write_text(body, encoding="utf-8")
        return root

    def test_a_scalar_comes_back_through_stdin_and_stdout(self, tmp_path: Path) -> None:
        root = self._workspace(tmp_path, "def value():\n    return 7.5\n")
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.exit_code == 0
        assert output.result is not None
        assert output.result.values[0].scalar == 7.5

    def test_pairs_come_back_as_naive_local_iso(self, tmp_path: Path) -> None:
        root = self._workspace(
            tmp_path,
            "import datetime\n"
            "def hourly():\n"
            "    return [(datetime.datetime(2024, 10, 27, 2, 0), 3.0)]\n",
        )
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:hourly"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is not None
        assert output.result.values[0].pairs == [("2024-10-27T02:00:00", 3.0)]

    def test_an_offset_bearing_timestamp_is_carried_not_stripped(self, tmp_path: Path) -> None:
        """The child must not "normalise" it. The two 02:00s of a fall-back day
        differ *only* by their offset, so stripping it here would hand them the
        same lookup key — the exact bug ``OffsetNotAllowed`` exists to refuse,
        reintroduced one process earlier where no test would be looking."""
        root = self._workspace(
            tmp_path,
            "import datetime\n"
            "def hourly():\n"
            "    tz = datetime.timezone(datetime.timedelta(hours=2))\n"
            "    return [(datetime.datetime(2024, 10, 27, 2, 0, tzinfo=tz), 3.0)]\n",
        )
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:hourly"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is not None
        assert output.result.values[0].pairs[0][0].endswith("+02:00")

    def test_a_callables_print_cannot_corrupt_the_envelope(self, tmp_path: Path) -> None:
        root = self._workspace(
            tmp_path,
            'def value():\n    print("hello from the analyst\'s code")\n    return 1.0\n',
        )
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is not None
        assert output.result.values[0].scalar == 1.0
        assert "hello from the analyst" in output.log

    def test_a_raising_callable_is_one_line_and_a_traceback_on_stderr(self, tmp_path: Path) -> None:
        root = self._workspace(tmp_path, 'def value():\n    raise ValueError("nope")\n')
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is not None
        value = output.result.values[0]
        assert value.ok is False
        assert "ValueError" in str(value.error)
        assert "Traceback" in output.log

    @pytest.mark.timeout(60)
    def test_a_hanging_callable_is_killed_at_the_ceiling(self, tmp_path: Path) -> None:
        root = self._workspace(tmp_path, "import time\ndef value():\n    time.sleep(120)\n")
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 2.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.exit_code is None
        assert output.result is None
        assert "killed" in output.log

    def test_the_import_closure_is_a_fact_not_a_guess(self, tmp_path: Path) -> None:
        """A conditional, ``importlib``-built import that static analysis would
        miss is reported all the same, because the child looks at what actually
        loaded."""
        root = self._workspace(
            tmp_path,
            "import importlib\n"
            "def value():\n"
            "    helper = importlib.import_module('secret_helper')\n"
            "    return helper.RATE\n",
        )
        (root / "secret_helper.py").write_text("RATE = 3.0\n", encoding="utf-8")
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 60.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is not None
        names = {Path(p).name for p in output.result.modules}
        assert {"mymod.py", "secret_helper.py"} <= names

    def test_a_child_that_cannot_start_is_a_no_exit_code_run(self, tmp_path: Path) -> None:
        root = self._workspace(tmp_path, "def value():\n    return 1\n")
        output = asyncio.run(
            SubprocessSpecRunner(("this-executable-does-not-exist",)).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 5.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.exit_code is None
        assert output.result is None
        assert "could not start" in output.log

    def test_a_callable_outside_the_workspace_is_refused_before_it_is_imported(
        self, tmp_path: Path
    ) -> None:
        """The child's own half of the "within the workspace" contract.

        Driven through the real entry module because the claim is about what
        Python's import machinery does, which a fake cannot have an opinion
        about: ``json`` is importable in any interpreter, has an attribute that
        answers, and lives nowhere near the workspace. It comes back as one
        ``ok: false`` line naming where it resolved — never as a value the gate
        would then reconcile a workbook against.
        """
        root = self._workspace(tmp_path, "def value():\n    return 1.0\n")
        output = asyncio.run(
            SubprocessSpecRunner(LOCAL_ARGV).run(
                SpecEntryRequest(callables=["json:dumps", "mymod:value"]),
                root,
                60.0,
                MAX_SPEC_LOG_BYTES,
            )
        )
        assert output.result is not None
        refused, allowed = output.result.values
        assert refused.ok is False
        assert "refused 'json'" in str(refused.error)
        # …and the workspace's own callable in the same request still answers.
        assert allowed.ok is True and allowed.scalar == 1.0

    @pytest.mark.timeout(90)
    def test_shutdown_kills_a_run_that_is_already_holding_a_subprocess(
        self, tmp_path: Path, store_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``main.py``'s shutdown comment, held.

        It claims "a debounced run that is mid-flight is cancelled rather than
        left holding a subprocess in the user's folder". It was not: a run left
        the pending map the instant it passed the debounce, so ``stop()`` could
        only ever reach one still *asleep* — the only kind with no child to
        orphan — and the runner had no cancellation branch to kill one anyway. A
        server stop (or a dev-server restart) landing on a live run left the
        analyst's code running unsupervised with nothing holding a handle to it.

        Driven with the **real** subprocess runner, because "the child is dead"
        is not a claim a fake can make.
        """
        root = tmp_path / "shutdown"
        root.mkdir()
        write_workbook(root / "models" / "budget.xlsx", GOOD)
        (root / "slow.py").write_text(
            "import time\n\n\ndef value():\n    time.sleep(120)\n    return 1.0\n",
            encoding="utf-8",
        )
        write_spec(
            root,
            "dispatch",
            """
workbook = "models/budget.xlsx"

[default_tolerance]
rel = 0.001

[[check]]
cell = "B2"
callable = "slow:value"
""",
        )
        spawned: list[asyncio.subprocess.Process] = []
        real_exec = asyncio.create_subprocess_exec

        async def recording(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
            proc = await real_exec(*args, **kwargs)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)

        async def scenario() -> None:
            bus = EventBus()
            validation = ValidationService(root, bus)
            validation.register(ReconciliationCheck())
            service = ReconcileSpecService(
                root,
                bus,
                SubprocessSpecRunner(LOCAL_ARGV),
                validation,
                SpecApprovalStore(store_dir),
                debounce_s=0.0,
            )
            approve(service)
            service.note_file_change(_change("models/budget.xlsx"))
            for _ in range(600):  # until the child is really up (never a sleep)
                await asyncio.sleep(0.05)
                if spawned:
                    break
            assert spawned, "the watcher never spawned a run to interrupt"
            assert _alive(spawned[0]), "the child finished before it could be killed"

            await service.stop()

            # Dead and reaped. Before the fix this assertion failed with the
            # child still sleeping out its two minutes in the user's folder.
            assert not _alive(spawned[0])
            assert service._running == set()

        asyncio.run(scenario())

    def test_stdout_that_is_not_an_envelope_is_a_named_failure_not_a_crash(
        self, tmp_path: Path
    ) -> None:
        root = self._workspace(tmp_path, "def value():\n    return 1\n")
        script = tmp_path / "noise.py"
        script.write_text("print('not json')\n", encoding="utf-8")
        output = asyncio.run(
            SubprocessSpecRunner((sys.executable, str(script))).run(
                SpecEntryRequest(callables=["mymod:value"]), root, 30.0, MAX_SPEC_LOG_BYTES
            )
        )
        assert output.result is None
        assert "not a spec envelope" in output.log


# --------------------------------------------------------------------------- REST


class TestTheRestSurface:
    def test_listing_specs_runs_nothing(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        seed(root)
        settings = Settings(
            workspace_root=root, app_data_root=tmp_path / "appdata", reconcile_fake=True
        )
        with TestClient(create_app(settings)) as client:
            body = client.get("/api/reconcile/specs").json()
            assert [s["status"] for s in body["specs"]] == ["unapproved"]
            assert "1 spec(s)" in body["detail"]
            digest = body["specs"][0]["digest"]

            stale = client.post(
                "/api/reconcile/specs/dispatch/approve",
                json={"approver": APPROVER, "digest": "not-the-digest"},
            )
            assert stale.status_code == 409

            approved = client.post(
                "/api/reconcile/specs/dispatch/approve",
                json={"approver": APPROVER, "digest": digest},
            )
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"
            assert approved.json()["approval"]["approver"] == APPROVER

            report = client.post("/api/reconcile/specs/dispatch/run").json()
            assert report["outcome"] == "pass"
            assert report["trigger"] == "manual"
            assert report["validation_id"]

            # …and the result really landed in the validation frame the panel reads.
            results = client.get("/api/validation").json()["results"]
            assert any(r["validation_id"] == report["validation_id"] for r in results)

            revoked = client.post("/api/reconcile/specs/dispatch/revoke").json()
            assert revoked["status"] == "unapproved"

    def test_an_unknown_spec_is_404(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        settings = Settings(
            workspace_root=root, app_data_root=tmp_path / "appdata", reconcile_fake=True
        )
        with TestClient(create_app(settings)) as client:
            assert client.post("/api/reconcile/specs/nope/revoke").status_code == 404
            assert (
                client.post(
                    "/api/reconcile/specs/nope/approve",
                    json={"approver": APPROVER, "digest": "x"},
                ).status_code
                == 404
            )
            # …and a run is a *report*, not an error: the refusal is the result.
            report = client.post("/api/reconcile/specs/nope/run").json()
            assert report["outcome"] == "blocked"

    def test_there_is_no_endpoint_that_accepts_a_spec_body(self, tmp_path: Path) -> None:
        """A spec is a checked-in file a reviewer can see in a diff. An endpoint
        that took one would be a way to run workspace code with nothing on disk
        to point at afterwards."""
        root = tmp_path / "ws"
        root.mkdir()
        settings = Settings(workspace_root=root, app_data_root=tmp_path / "appdata")
        app = create_app(settings)
        # Read from the published schema rather than by walking `app.routes`:
        # that is the contract a client sees, and it is the one an accidental
        # fifth verb would appear in.
        paths = app.openapi()["paths"]
        reconcile = {
            path: set(operations)
            for path, operations in paths.items()
            if path.startswith("/api/reconcile")
        }
        assert reconcile == {
            "/api/reconcile/specs": {"get"},
            "/api/reconcile/specs/{name}/approve": {"post"},
            "/api/reconcile/specs/{name}/revoke": {"post"},
            "/api/reconcile/specs/{name}/run": {"post"},
        }
        # …and the only body any of them accepts names an approver and a digest.
        approve_body = paths["/api/reconcile/specs/{name}/approve"]["post"]["requestBody"]
        schema_ref = approve_body["content"]["application/json"]["schema"]["$ref"]
        properties = app.openapi()["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]][
            "properties"
        ]
        assert set(properties) == {"approver", "digest"}

    def test_workspace_gates_json_is_still_refused(self, tmp_path: Path) -> None:
        """PR-B builds the one-time trust prompt that
        ``gates.workspace_config_refusal`` names as the price of admission — and
        spends it on reconciliation specs only. Per-workspace *gate*
        configuration stays refused, out loud, in the same words."""
        from workbench_server.services.gates import workspace_config_refusal

        root = tmp_path / "ws"
        (root / ".workbench").mkdir(parents=True)
        (root / ".workbench" / "gates.json").write_text("{}", encoding="utf-8")
        refusal = workspace_config_refusal(root)
        assert refusal is not None
        assert refusal.outcome == "skipped"
        assert "deliberately not read" in refusal.detail


# --------------------------------------------------------------------------- the moment


class TestTheSaveToChipFlipLoop:
    """**Save the workbook; seconds later the chip flips. Nobody fires a check.**

    The whole product claim of PR-B, driven the way a user hits it: the real
    watcher, the real bus, the real ``ReconciliationCheck``, and a real file
    written to disk. Only the callable is scripted (``WORKBENCH_RECONCILE_FAKE``),
    because CI must not execute workspace code.
    """

    @pytest.mark.timeout(90)
    def test_a_wrong_number_written_to_the_workbook_flips_the_chip(self, tmp_path: Path) -> None:
        root = tmp_path / "ws"
        root.mkdir()
        seed(root)
        settings = Settings(
            workspace_root=root,
            app_data_root=tmp_path / "appdata",
            reconcile_fake=True,
            reconcile_debounce_s=0.2,
        )
        with TestClient(create_app(settings)) as client:
            listed = client.get("/api/reconcile/specs").json()["specs"][0]
            client.post(
                "/api/reconcile/specs/dispatch/approve",
                json={"approver": APPROVER, "digest": listed["digest"]},
            )
            # The baseline: the workbook and the code agree.
            assert client.post("/api/reconcile/specs/dispatch/run").json()["outcome"] == "pass"

            with client.websocket_connect("/ws/events") as ws:
                # The user's own gesture, and the only one in this test: save the
                # workbook with a wrong number in it.
                write_workbook(root / "models" / "budget.xlsx", 9999.0)
                flipped = _await_spec_event(ws, trigger="watcher")

        assert flipped["state"]["last_run"]["outcome"] == "fail"
        assert flipped["state"]["last_run"]["trigger"] == "watcher"
        assert flipped["state"]["status"] == "approved"


def _await_spec_event(ws: Any, *, trigger: str, budget: int = 60) -> dict[str, Any]:
    """The next ``reconcile_spec`` frame carrying a run with this trigger.

    Bounded by frame count rather than by a sleep: the watcher publishes several
    ``file_changed`` events for one save on Windows, and the one this journey is
    about is the run that follows them.
    """
    for _ in range(budget):
        event = json.loads(ws.receive_text())
        if event.get("type") != "reconcile_spec":
            continue
        last_run = event["state"].get("last_run")
        if last_run is not None and last_run["trigger"] == trigger:
            return dict(event)
    raise AssertionError(f"no reconcile_spec run with trigger={trigger} in {budget} frames")


# --------------------------------------------------------------------------- budgets


class TestBudgets:
    def test_a_full_spec_state_stays_small_enough_to_ride_the_bus(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """A ``SpecEvent`` is fanned out to every window on every run, so its
        size is a cost paid per save rather than per request. Measured at ~1.1 KB
        for an approved spec covering three files; the ceiling is that plus room
        for a longer refusal sentence."""
        helper = workspace / "se3" / "_helpers.py"
        helper.write_text("RATE = 1.0\n", encoding="utf-8")
        service, _, _ = build_service(workspace, store_dir, FakeSpecRunner(modules=[str(helper)]))
        approve(service)
        asyncio.run(service.run("dispatch"))
        state = service.state("dispatch")
        assert state is not None
        assert len(state.model_dump_json().encode()) < 2_048

    def test_the_captured_log_window_is_the_gates_one(self) -> None:
        assert MAX_SPEC_LOG_BYTES == 8_192


# --------------------------------------------------------------------------- wiring


class TestWiring:
    def test_the_service_is_constructed_started_and_re_rooted(self, tmp_path: Path) -> None:
        """A service that copies ``workspace.root`` must be in ``create_app``'s
        ``WorkspaceService`` construction, or it keeps serving the folder the
        user left (CLAUDE.md). Asserted rather than reviewed."""
        root = tmp_path / "ws"
        root.mkdir()
        seed(root)
        other = tmp_path / "other"
        other.mkdir()
        settings = Settings(
            workspace_root=root, app_data_root=tmp_path / "appdata", reconcile_fake=True
        )
        with TestClient(create_app(settings)) as client:
            assert client.get("/api/reconcile/specs").json()["specs"][0]["name"] == "dispatch"
            moved = client.post("/api/workspace/switch", json={"path": str(other)})
            assert moved.status_code == 200
            assert client.get("/api/reconcile/specs").json()["specs"] == []

    def test_a_run_stamps_a_naive_local_clock(self, workspace: Path, store_dir: Path) -> None:
        """Naive local wall clock, matching the reconciliation module's own
        contract — never a UTC instant a reader has to convert in their head."""
        service, _, _ = build_service(workspace, store_dir)
        approve(service)
        report = asyncio.run(service.run("dispatch"))
        assert isinstance(report.ran_at, datetime)
        assert report.ran_at.tzinfo is None

    def test_the_validation_spec_names_only_the_reconciliation_check(
        self, workspace: Path, store_dir: Path
    ) -> None:
        """It hands values to the gate that already exists and asks for nothing
        else — not the toolchain gates, not the reviewer."""
        seen: list[ValidationSpec] = []

        class Recording:
            async def run(self, spec: ValidationSpec) -> ValidationResult:
                seen.append(spec)
                real = ValidationService(workspace, EventBus())
                real.register(ReconciliationCheck())
                return await real.run(spec)

        bus = EventBus()
        service = ReconcileSpecService(
            workspace,
            bus,
            FakeSpecRunner(),
            Recording(),
            SpecApprovalStore(store_dir),
            debounce_s=0.0,
        )
        approve(service)
        asyncio.run(service.run("dispatch"))
        assert [spec.checks for spec in seen] == [["reconciliation"]]
        assert seen[0].subject.kind == "file"
        assert seen[0].subject.ref == "models/budget.xlsx"
