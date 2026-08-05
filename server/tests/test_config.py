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


def test_fake_agent_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scripted replies must never be the default — only an explicit env flag."""
    assert Settings().fake_agent is False
    monkeypatch.setenv("WORKBENCH_FAKE_AGENT", "1")
    assert Settings().fake_agent is True


def test_office_hosting_defaults_to_auto_and_the_fake_backend_is_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` currently resolves to *not* hosting natively (owner decision), and
    a simulated host must never be the default — same bar as the fake agent."""
    assert Settings().office_native == "auto"
    assert Settings().office_fake is False
    monkeypatch.setenv("WORKBENCH_OFFICE_NATIVE", "on")
    monkeypatch.setenv("WORKBENCH_OFFICE_FAKE", "1")
    assert Settings().office_native == "on"
    assert Settings().office_fake is True
