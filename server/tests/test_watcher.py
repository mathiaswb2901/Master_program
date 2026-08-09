"""Full-pipeline watcher test: write through the API -> event arrives on /ws/events."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.files import FileChangedEvent
from workbench_server.services import watcher
from workbench_server.services.event_bus import EventBus
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


# ---------------------------------------------------- hashing belongs on a worker
#
# The digest a `file_changed` event carries is a whole file read plus a SHA-256, up
# to `MAX_TEXT_FILE_BYTES` (5 MB). It used to run inline in the watcher coroutine,
# which is the same defect the PTY, gates and voice lanes each had to fix: work that
# blocks measured in hundreds of milliseconds, on the one thread every WebSocket
# frame and every HTTP response also has to pass through. An analyst saving a big
# CSV out of a model stalled the whole window.

#: How long the stand-in digest blocks. Long enough that a loop it ran on could not
#: possibly answer inside `RESPONSIVE_S`, short enough not to lengthen the suite.
SLOW_HASH_S = 3.0

#: What a request served *while* that digest is in flight is allowed to cost. Two
#: orders below `SLOW_HASH_S`; on a loop that was blocked it would be ~`SLOW_HASH_S`.
RESPONSIVE_S = 1.0


@pytest.mark.timeout(120)
def test_a_slow_hash_does_not_stall_the_server(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction, from where a user feels it: save a file, ask the server
    something, and get an answer while the digest is still being computed.

    The stand-in blocks for `SLOW_HASH_S` where a real 5 MB read blocks for a
    fraction of that — same shape, deterministic, and no multi-megabyte fixture.
    On master the `/api/health` below waits out the whole sleep, because the sleep
    is happening on the loop that would have served it.
    """
    started = threading.Event()
    hashed_on: list[int] = []

    def slow_hash(path: Path) -> str | None:
        hashed_on.append(threading.get_ident())
        started.set()
        time.sleep(SLOW_HASH_S)
        return content_hash(path.read_bytes())

    monkeypatch.setattr(watcher, "_hash_of", slow_hash)

    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws/events") as ws:
        (tmp_path / "se3-2026.csv").write_bytes(b"hour,mw\n1,42\n")
        assert started.wait(60), "the watcher never reached the digest"

        # Mid-digest, and the server has to still be a server.
        at = time.perf_counter()
        assert client.get("/api/health").status_code == 200
        served_in = time.perf_counter() - at
        assert served_in < RESPONSIVE_S, (
            f"a request took {served_in:.2f}s while one file was being hashed; "
            "the digest is back on the event loop"
        )

        event = json.loads(ws.receive_text())
        assert event["path"] == "se3-2026.csv"
        # Off the loop, but not off the *result*: the event still carries the hash.
        assert event["hash"] == content_hash(b"hour,mw\n1,42\n")

    # This test's thread is the client's, not the loop's, so it cannot name the
    # loop by id — the responsiveness assertion above is what carries the weight
    # here, and the next test makes the identity claim where it can be made.
    assert hashed_on, "the digest never ran"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_the_digest_never_runs_on_the_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread-identity assertion, made directly against the `Watcher`.

    An async test body runs *on* the event loop, so `threading.get_ident()` here
    is the loop's thread id — the same trick `test_pty_manager` plays with
    `FakePosix.slept_on`. A digest that shows up with this id is a digest that
    blocked every other coroutine in the process.
    """
    hashed_on: list[int] = []

    def recording_hash(path: Path) -> str | None:
        hashed_on.append(threading.get_ident())
        return content_hash(path.read_bytes())

    monkeypatch.setattr(watcher, "_hash_of", recording_hash)

    bus = EventBus()
    events = bus.subscribe()
    w = watcher.Watcher(tmp_path, bus)
    w.start()
    try:
        await asyncio.sleep(0.3)  # let the watch attach before the first write
        (tmp_path / "prices.csv").write_bytes(b"hour,eur\n")
        event = await asyncio.wait_for(events.get(), 60)
    finally:
        await w.stop()

    assert isinstance(event, FileChangedEvent)
    assert event.path == "prices.csv"
    assert event.hash == content_hash(b"hour,eur\n")
    assert hashed_on, "the digest never ran"
    assert threading.get_ident() not in hashed_on, "the digest ran on the event loop"


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_slow_digest_does_not_let_later_events_overtake_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hashes now finish out of order; events still do not.

    Overlapping the reads is the whole speedup, and it is also the whole risk: a
    client patches its tree from this stream, so a fast small file must not
    overtake the slow large one written before it. The first file's digest is made
    to block for `SLOW_HASH_S` and the second's to return instantly, and the
    second is written after the debounce window so the two land in different
    batches — the ordering the watcher actually promises. Publication order is the
    order the changes were seen, not the order the hashes came back.
    """

    def staged_hash(path: Path) -> str | None:
        if path.name == "slow.csv":
            time.sleep(SLOW_HASH_S)
        return content_hash(path.read_bytes())

    monkeypatch.setattr(watcher, "_hash_of", staged_hash)

    bus = EventBus()
    events = bus.subscribe()
    w = watcher.Watcher(tmp_path, bus)
    w.start()
    try:
        await asyncio.sleep(0.3)
        (tmp_path / "slow.csv").write_bytes(b"big\n")
        await asyncio.sleep(0.6)  # past the 200 ms debounce: its own batch
        (tmp_path / "fast.csv").write_bytes(b"small\n")
        seen: list[str] = []
        while "fast.csv" not in seen:
            event = await asyncio.wait_for(events.get(), 60)
            if isinstance(event, FileChangedEvent):
                seen.append(event.path)
    finally:
        await w.stop()

    slow_at, fast_at = seen.index("slow.csv"), seen.index("fast.csv")
    assert slow_at < fast_at, f"the fast file overtook the slow one: {seen}"
