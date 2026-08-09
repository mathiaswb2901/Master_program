"""Guard the 3-OS CI matrix (`.github/workflows/ci.yml`, M7 §C2).

A workflow file cannot test itself: the only run that proves it is the one that
already merged. So the shape that C2 is *about* is pinned here, statically, in
the gate that runs on every PR — which OSes the cross-platform jobs cover, which
jobs are deliberately Windows-only, and that the aggregating gate still needs
every one of them.

The claim these tests cannot make is that the legs pass; that is the CI run's
job. What they can make impossible is the silent regression — someone dropping
ubuntu from the matrix to make a red leg go away, or adding a job that the
quality gate never waits on.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # pyyaml is a transitive dep; no stubs installed

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

#: The jobs C2 takes cross-platform, and the three runners they must cover.
MATRIXED = ("server", "ui")
REQUIRED_OSES = {"windows-latest", "ubuntu-latest", "macos-latest"}
#: Deliberately Windows-only, each for a reason written into the workflow:
#: native Office/Tauri window hosting (desktop, e2e) or a pinned perf baseline.
WINDOWS_ONLY = ("desktop", "e2e", "perf")


def _ci() -> dict[str, Any]:
    parsed = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "ci.yml did not parse to a mapping"
    return parsed


def test_ci_yaml_is_valid() -> None:
    assert "quality-gate" in _ci()["jobs"], "the aggregating gate disappeared"


def test_the_cross_platform_jobs_cover_all_three_runners() -> None:
    jobs = _ci()["jobs"]
    for name in MATRIXED:
        matrix = jobs[name]["strategy"]["matrix"]
        assert set(matrix["os"]) == REQUIRED_OSES, f"{name} no longer covers all three OSes"
        assert jobs[name]["runs-on"] == "${{ matrix.os }}", f"{name} pins a runner past its matrix"


def test_a_broken_leg_is_not_cancelled_away() -> None:
    """`fail-fast: false` is what makes a red matrix diagnosable, not just red."""
    jobs = _ci()["jobs"]
    for name in MATRIXED:
        assert jobs[name]["strategy"]["fail-fast"] is False, f"{name} cancels its sibling legs"


def test_the_windows_only_jobs_stay_windows_only() -> None:
    """Native window hosting and the pinned perf baseline are not matrixable."""
    jobs = _ci()["jobs"]
    for name in WINDOWS_ONLY:
        assert jobs[name]["runs-on"] == "windows-latest", f"{name} left windows-latest"
        assert "strategy" not in jobs[name], f"{name} grew a matrix it cannot honour"


def test_the_quality_gate_waits_on_every_job() -> None:
    """A job the gate does not `need` is a job whose failure merges anyway.

    A matrix job contributes one aggregate result to `needs`, so naming
    `server` once covers all three of its legs.
    """
    jobs = _ci()["jobs"]
    gate = jobs["quality-gate"]
    needed = set(gate["needs"])
    others = set(jobs) - {"quality-gate"}
    assert needed == others, f"quality-gate ignores {others - needed}"
    assert gate["if"] is True or str(gate["if"]).strip() == "always()"


def test_only_the_path_filtered_job_can_ever_be_skipped() -> None:
    """The gate's `skipped is allowed` carve-out has exactly one beneficiary.

    `quality-gate` runs with `if: always()` and passes a job whose result is
    `skipped`, because `desktop` is path-filtered — a PR that touches no Rust
    must not pay minutes of `cargo build`, and a skip there is the filter
    working. That carve-out is safe only while `desktop` is the *only* job that
    can be skipped at all: give `server` or `e2e` a condition and its result
    turns into `skipped` instead of `success`, which the gate would wave
    through. Nothing else in the workflow says so, so it is said here.

    A job's own `if:` is the only way to reach that state. `needs:` cannot
    (`changes` is the sole dependency of `desktop`, and a skipped dependency is
    what `desktop`'s filter is *for*), and a step-level `if:` — the `ui` job
    uses several — decides nothing about the job's result.
    """
    jobs = _ci()["jobs"]
    conditional = {name for name, job in jobs.items() if "if" in job}
    assert conditional <= {"desktop", "quality-gate"}, (
        f"{conditional - {'desktop', 'quality-gate'}} can now be skipped, and "
        "quality-gate treats a skip as a pass"
    )
