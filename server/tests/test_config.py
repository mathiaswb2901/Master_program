from pathlib import Path

import pytest

from workbench_server.config import Settings


def test_defaults_resolve_workspace_to_cwd() -> None:
    s = Settings()
    assert s.resolved_workspace() == Path.cwd().resolve()
    assert s.port == 8787
    assert s.onlyoffice_url is None  # office editing is optional by design


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKBENCH_PORT", "9999")
    monkeypatch.setenv("WORKBENCH_LOG_LEVEL", "debug")
    s = Settings()
    assert s.port == 9999
    assert s.log_level == "debug"


def test_explicit_workspace_root(tmp_path: Path) -> None:
    s = Settings(workspace_root=tmp_path)
    assert s.resolved_workspace() == tmp_path.resolve()
