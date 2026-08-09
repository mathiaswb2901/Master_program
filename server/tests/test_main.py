"""The entrypoint's runtime-token file lifecycle (M5 item 8 / OSS-bar item 1).

``run()`` drops the per-launch token in a file an attaching desktop shell can
read, and removes it on the way out so a token never outlives its process. The
write/chmod/unlink dance is security-relevant and easy to regress, so it is
pinned here: uvicorn.run is stubbed to a callback that inspects the file while
the server is "serving", and the finally is exercised by making the stub raise.
"""

from pathlib import Path

import pytest

from workbench_server import main
from workbench_server.config import Settings
from workbench_server.main import run
from workbench_server.runtime import runtime_token_path


@pytest.fixture
def run_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings that keep the token file under a throwaway app-data root, and
    make ``run()`` use them without reading the ambient environment."""
    settings = Settings(workspace_root=tmp_path, app_data_root=tmp_path / "appdata", port=8787)
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    return settings


def test_runtime_token_path_is_keyed_by_port(tmp_path: Path) -> None:
    # Finding 4: two instances on different ports must not share the file.
    a = Settings(workspace_root=tmp_path, app_data_root=tmp_path / "d", port=8787)
    b = Settings(workspace_root=tmp_path, app_data_root=tmp_path / "d", port=8788)
    assert runtime_token_path(a) != runtime_token_path(b)
    assert runtime_token_path(a).name == "auth-token-8787"


def test_run_writes_token_while_serving_then_removes_it(
    run_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = runtime_token_path(run_settings)
    observed: dict[str, str] = {}

    def fake_serve(app: object, **kwargs: object) -> None:
        # Stand in for uvicorn.run: the server is "up", so the token file must
        # exist and hold exactly the token minted onto app.state.
        observed["content"] = token_path.read_text(encoding="utf-8")
        observed["app_token"] = app.state.auth_token  # type: ignore[attr-defined]

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_serve)

    run()

    assert observed["content"] == observed["app_token"]
    assert observed["content"]  # non-empty
    # Removed on the way out: no token outlives the process that owns it.
    assert not token_path.exists()


def test_run_removes_token_even_when_serving_raises(
    run_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = runtime_token_path(run_settings)

    def boom(app: object, **kwargs: object) -> None:
        assert token_path.exists()  # written before serving starts
        raise RuntimeError("serve exploded")

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", boom)

    with pytest.raises(RuntimeError, match="serve exploded"):
        run()

    # The finally must still have unlinked it.
    assert not token_path.exists()
