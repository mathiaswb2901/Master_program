"""Guard the release pipeline (`.github/workflows/release.yml`).

Push-tag / `workflow_dispatch` triggers mean this workflow never runs on a PR,
so nothing else in the quality gate catches a rename that breaks the release
path. These are cheap static checks: the workflow is valid YAML, the files it
shells out to still exist, the desktop bundle still emits the targets its
`files:` globs collect, and its toolchain still matches `ci.yml` so the release
build cannot drift from what CI proves green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # pyyaml is a transitive dep; no stubs installed

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_YML = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load(path: Path) -> dict[str, Any]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{path.name} did not parse to a mapping"
    return parsed


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    return list(workflow["jobs"][job]["steps"])


def _uses_versions(steps: list[dict[str, Any]]) -> dict[str, str]:
    """Map action name -> pinned version from a job's `uses:` steps."""
    versions: dict[str, str] = {}
    for step in steps:
        uses = step.get("uses")
        if uses and "@" in uses:
            name, version = uses.rsplit("@", 1)
            versions[name] = version
    return versions


def test_release_yaml_is_valid() -> None:
    workflow = _load(RELEASE_YML)
    assert "installer" in workflow["jobs"], "the installer job disappeared"


def test_release_shells_out_to_files_that_exist() -> None:
    """The `run:` steps invoke real on-disk scripts — break CI on a rename."""
    for rel in (
        Path("packaging") / "build_backend.ps1",
        Path("packaging") / "workbench-server.spec",
    ):
        assert (REPO_ROOT / rel).is_file(), f"release.yml references missing {rel}"


def test_release_uploads_targets_the_bundle_still_produces() -> None:
    """The `files:` globs must match targets tauri is configured to build."""
    workflow = _load(RELEASE_YML)
    publish = next(
        s
        for s in _steps(workflow, "installer")
        if str(s.get("uses", "")).startswith("softprops/action-gh-release")
    )
    files = publish["with"]["files"]
    assert "bundle/nsis/" in files and ".exe" in files, "nsis installer no longer uploaded"
    assert "bundle/msi/" in files and ".msi" in files, "msi installer no longer uploaded"

    tauri_conf = _load(REPO_ROOT / "desktop" / "src-tauri" / "tauri.conf.json")
    targets = tauri_conf["bundle"]["targets"]
    # "all" emits both nsis and msi on Windows; an explicit list must name them.
    if targets != "all":
        produces_both = "nsis" in targets and "msi" in targets
        assert produces_both, f"tauri targets {targets!r} drop the nsis+msi release.yml uploads"


def test_release_toolchain_matches_ci() -> None:
    """The release job pins the same action versions the CI gate proves green."""
    ci = _load(CI_YML)
    release_uses = _uses_versions(_steps(_load(RELEASE_YML), "installer"))
    ci_uses: dict[str, str] = {}
    for job in ci["jobs"].values():
        steps = job.get("steps")
        if isinstance(steps, list):
            ci_uses.update(_uses_versions(steps))

    for action in ("astral-sh/setup-uv", "actions/setup-node", "dtolnay/rust-toolchain"):
        assert action in release_uses, f"release.yml no longer sets up {action}"
        rel, cin = release_uses[action], ci_uses[action]
        assert rel == cin, f"{action} drifted: release pins {rel}, ci pins {cin}"
