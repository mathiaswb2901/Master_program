"""Full-pipeline watcher test: write through the API -> event arrives on /ws/events."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.workspace import content_hash


@pytest.mark.timeout(60)
def test_api_write_reaches_events_websocket(settings: Settings, tmp_path: Path) -> None:
    app = create_app(settings)
    # lifespan starts the real watcher
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        resp = client.put(
            "/api/files/content",
            json={"path": "report.md", "content": "# Q3\n"},
        )
        assert resp.status_code == 200
        event = json.loads(ws.receive_text())
        assert event["type"] == "file_changed"
        # atomic-write .tmp artifacts must never surface — first event is the real file
        assert event["path"] == "report.md"
        assert event["change"] in ("added", "modified")
        # hash lets the writing client recognize its own echo
        assert event["hash"] == content_hash(b"# Q3\n")


@pytest.mark.timeout(60)
def test_external_edit_is_observed(settings: Settings, tmp_path: Path) -> None:
    """Simulates an agent editing a file directly on disk."""
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        # write_bytes: Path.write_text would translate \n -> \r\n on Windows
        (tmp_path / "model.py").write_bytes(b"VERSION = 2\n")
        event = json.loads(ws.receive_text())
        assert event["path"] == "model.py"
        assert event["hash"] == content_hash(b"VERSION = 2\n")


@pytest.mark.timeout(60)
def test_a_directory_created_while_watching_is_announced(
    settings: Settings, tmp_path: Path
) -> None:
    """A new folder is a row the tree can patch in, so it gets its own event.

    Its counterpart is the next test: the same event has to *stop* once the
    folder declares itself a build cache. Both halves are the rule in
    `Watcher._skip`, and this is the half that says "visible" — without it, a
    folder an agent or a build creates stays invisible until something forces a
    full refetch, and so does every file created inside it (the client has no
    row to hang them on).
    """
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        (tmp_path / "notebooks").mkdir()
        event = json.loads(ws.receive_text())
        assert event["path"] == "notebooks"
        assert event["change"] == "added"
        assert event["is_dir"] is True
        # A directory has no content to hash, and offering one would invite a
        # client to compare it against something.
        assert event["hash"] is None


@pytest.mark.timeout(60)
def test_build_cache_tagged_while_watching_produces_no_file_events(
    settings: Settings, tmp_path: Path
) -> None:
    """A cargo build tagging its output under a running server: churn stops dead.

    The memo in `IgnoreIndex` is what is really on trial. `early.rlib` below is
    written while `target` is still untagged, so the memo is *asked* about that
    directory and correctly answers "ordinary folder" — an untagged `target` is
    indistinguishable from the analyst's own (the last test in this file). The
    tag then arrives inside the same debounce batch as the twenty artifacts it
    governs, and every one of them is suppressed only if the watcher notices the
    tag *before* judging that batch and throws the stale verdict away. A memo
    that is merely cold would pass a weaker version of this test; this one hands
    it a wrong answer first and requires it to be dropped.

    `early.rlib` is therefore a barrier as well as a setup step: waiting for its
    event proves both that the memo now holds the stale verdict and that the
    watch on the deepest directory is live on this platform. That second half is
    why the tree is created *before* the server starts, which is also how a real
    build finds it — `target/` outlives the build that made it.
    `ReadDirectoryChangesW` watches a tree recursively in the kernel, so on
    Windows a directory is watched the instant it exists; inotify adds a watch
    per directory, and only once it has *processed* that directory's creation.
    A tag written microseconds after `mkdir` returns can therefore land in a
    directory linux is not yet watching and never be delivered at all. A real
    cargo build spends seconds between the two, so that race is a test's to
    avoid, not a bug to assert around — and starting from a tree that already
    exists removes it on all three platforms instead of tuning a sleep for one.

    The only frame allowed between the barrier and the sentinel is the
    `tree_invalidated` notice the tag itself raises: the signal that tells a
    client with an incrementally patched tree to re-read what it is showing,
    since the events that would have described the change are exactly the ones
    now being suppressed.
    """
    build = tmp_path / "desktop" / "src-tauri" / "target"
    (build / "debug" / "build").mkdir(parents=True)
    barrier = "desktop/src-tauri/target/debug/build/early.rlib"

    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        (build / "debug" / "build" / "early.rlib").write_bytes(b"\x00" * 64)
        while True:
            event = json.loads(ws.receive_text())
            if event.get("path") == barrier:
                assert event["type"] == "file_changed"
                break

        (build / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55\n")
        for i in range(20):
            (build / "debug" / "build" / f"artifact{i}.rlib").write_bytes(b"\x00" * 64)
        # Written last: the sentinel that says every earlier event has been seen.
        # The watcher preserves order, so anything the cache leaked arrives first.
        (tmp_path / "model.py").write_bytes(b"VERSION = 3\n")

        seen: list[dict[str, object]] = []
        while True:
            event = json.loads(ws.receive_text())
            if event.get("path") == "model.py":
                break
            # A second frame for the barrier file — `added` then `modified` for
            # one write, or a coalesced repeat — is the one thing that may
            # legitimately still be in flight here, and it predates the tag.
            if event.get("path") == barrier:
                continue
            seen.append(event)

        assert [e for e in seen if e["type"] == "tree_invalidated"], "the tag was never announced"
        # Everything else written in that window was inside a directory that had
        # already declared itself disposable, so *nothing* may have surfaced —
        # not the artifacts, not the tag file, not a directory frame.
        leaked = [e for e in seen if e["type"] == "file_changed"]
        assert not leaked, f"the tagged cache leaked events: {leaked}"


@pytest.mark.timeout(60)
def test_the_users_own_target_folder_still_reports_changes(
    settings: Settings, tmp_path: Path
) -> None:
    """The same name, untagged, stays fully live — analysts edit these files."""
    (tmp_path / "analysis" / "target").mkdir(parents=True)
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        (tmp_path / "analysis" / "target" / "se3-2026.csv").write_bytes(b"hour,mw\n")
        event = json.loads(ws.receive_text())
        assert event["path"] == "analysis/target/se3-2026.csv"
