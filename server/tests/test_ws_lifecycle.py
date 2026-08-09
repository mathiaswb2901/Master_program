"""What the live WebSockets do when the peer disappears mid-frame.

Three regressions, all reproduced the way a user hits them — through the real
routers, over a real socket, with the real `PtyManager`/`SessionManager` behind
them — because the bugs are in the *teardown path* and a unit test that calls
`release()` directly would pass on the broken code.

The peer "disappearing" is spelled here as a `ConnectionResetError` out of
`ws.send_text`, which is what an abrupt TCP drop (a killed browser, a laptop
lid, a dropped Wi-Fi link) surfaces as. The shipped routers suppressed only
`RuntimeError` around that send, so the pump task ended up *holding* the
exception, the `await pump` in the endpoint's `finally:` re-raised it, and the
line after it — the one that hands the OS its child process back, or drops the
listener queue — never ran.

`server/tests/test_pty_manager.py` holds the other half: that the release
itself waits on a worker thread and that a shutdown reaps every wedged terminal
at once.
"""

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient

from tests.test_agent_sessions import ResultMessage, delta, make_factory
from tests.test_pty_manager import WedgedChild, posix_pty
from tests.test_terminal import ScriptedProc
from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services import pty_posix
from workbench_server.services.agent_sessions import SessionManager
from workbench_server.services.ws_lifecycle import drain_pump


@pytest.fixture(autouse=True)
def _no_unreaped_leak_between_tests() -> Iterator[None]:
    """A child that outlives its reap timeout is parked in a module-level list."""
    pty_posix._UNREAPED.clear()
    yield
    pty_posix._UNREAPED.clear()


def eventually(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    """Poll a teardown that now finishes on another thread.

    Not a sleep-and-hope: the endpoint's `finally:` runs after the client has
    already been told the socket is closed, so "immediately after the `with`"
    is a race in *either* direction. A timeout this long only ever elapses when
    the cleanup genuinely never happens — which is the bug under test.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def reset_on_send(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Make every outbound frame die the way an abrupt TCP drop dies.

    Patched on the class, not the instance: the endpoint owns its `WebSocket`
    and never hands it out. `accept()` goes through `send()` rather than
    `send_text()`, so the handshake still completes and only the pump is hit.
    """
    attempted = threading.Event()

    async def reset(self: WebSocket, data: str) -> None:
        attempted.set()
        raise ConnectionResetError(10054, "An existing connection was forcibly closed")

    monkeypatch.setattr(WebSocket, "send_text", reset)
    return attempted


class AnnouncingProc(ScriptedProc):
    """A scripted shell that says when it was terminated.

    The teardown is off-thread now, so closing the client socket and asserting
    on the next line would be a race. `terminated_at` is the handshake — and
    waiting on it is also what makes "it never happened" a 10-second failure
    rather than a flake.
    """

    def __init__(self, chunks: list[str]) -> None:
        super().__init__(chunks)
        self.terminated_at = threading.Event()

    def terminate(self, force: bool = False) -> bool:
        done = super().terminate(force)
        self.terminated_at.set()
        return done


class AnnouncingWedgedChild(WedgedChild):
    """`WedgedChild`, plus the same handshake — its fd close ends the release."""

    def __init__(self) -> None:
        super().__init__()
        self.released_at = threading.Event()

    def close(self, fd: int) -> None:
        super().close(fd)
        self.released_at.set()


@pytest.mark.timeout(60)
class TestTheTerminalSocket:
    def test_a_reset_mid_send_still_releases_the_pty(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak: an orphaned shell process and a session the manager keeps.

        On master the pump stored the `ConnectionResetError`, `await pump`
        re-raised it out of the `finally:`, and `manager.release(session)` on
        the next line never ran — so `terminate()` was never called on the
        child and the session stayed in `_sessions` for the life of the server.
        """
        proc = AnnouncingProc(["hello"] * 8)
        monkeypatch.setattr(
            "workbench_server.services.pty_manager._spawn_backend",
            lambda cwd, rows, cols: proc,
        )
        attempted = reset_on_send(monkeypatch)

        app = create_app(settings)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/terminal") as ws:
                assert attempted.wait(10), "the pump never got as far as a send"
                ws.send_text(json.dumps({"type": "input", "data": "echo hi\r"}))
            # leaving the context closed the socket, so teardown is under way
            assert proc.terminated_at.wait(10), "the shell process was orphaned"
            assert eventually(lambda: app.state.pty_manager._sessions == {})

    def test_releasing_a_wedged_pty_never_runs_on_the_event_loop_thread(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The stall: one hung `ssh` freezing every other socket on the server.

        `spawn` runs inside the endpoint, so the factory records the loop
        thread; the child is one that can never be reaped, so the release polls
        with backoff for a whole `REAP_TIMEOUT_S`. Those sleeps landing on the
        recorded thread is the bug — a second in which uvicorn answers nothing.
        """
        fake = AnnouncingWedgedChild()
        loop_thread: list[int] = []

        def spawn_backend(cwd: Path, rows: int, cols: int) -> object:
            loop_thread.append(threading.get_ident())  # the endpoint's own thread
            return posix_pty(fake)

        monkeypatch.setattr("workbench_server.services.pty_manager._spawn_backend", spawn_backend)

        app = create_app(settings)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/terminal") as ws:
                # the fake reads EOF at once, so the pump sends the exit frame
                # and finishes; receiving it proves the socket was live.
                assert json.loads(ws.receive_text())["type"] == "exit"
            assert fake.released_at.wait(10)
            assert eventually(lambda: app.state.pty_manager._sessions == {})

        assert fake.slept, "the wedged child never reached the polling reap"
        assert loop_thread, "the endpoint never spawned a session"
        assert loop_thread[0] not in fake.slept_on


@pytest.mark.timeout(60)
class TestTheAgentSocket:
    def test_a_reset_mid_send_still_unsubscribes_the_listener(
        self, settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak: a queue in the session's fan-out that nobody will ever read.

        Same shape as the terminal's, one resource down: `session.unsubscribe`
        sat on the line after the re-raising `await`, so a client that dropped
        abruptly left its queue in `_listeners` forever. Every later frame was
        then serialized and pushed into it until it hit its cap.
        """
        app = create_app(settings)
        app.state.session_manager = SessionManager(
            tmp_path, make_factory([delta("hi"), ResultMessage()]), max_sessions=4
        )
        attempted = reset_on_send(monkeypatch)

        with TestClient(app) as client:
            created = client.post("/api/agents/sessions", json={"folder": ""})
            local_id = created.json()["session_id"]
            session = app.state.session_manager.get(local_id)
            assert session is not None
            with client.websocket_connect(f"/ws/agent/{local_id}") as ws:
                ws.send_text(json.dumps({"type": "user_message", "text": "hi"}))
                assert attempted.wait(10), "the pump never got as far as a send"
            # the private set is the leak itself — there is no public reading of it
            assert eventually(lambda: session._listeners == set())


class TestDrainPump:
    """The unit under both regressions, stated once."""

    async def test_a_pump_that_died_of_a_reset_does_not_escape_the_drain(self) -> None:
        async def died() -> None:
            raise ConnectionResetError(10054, "forcibly closed")

        task = asyncio.create_task(died())
        await asyncio.sleep(0)  # let it fail, so the drain is cancelling a *done* task
        await drain_pump(task, stream="test")

    async def test_a_pump_that_died_of_a_bug_is_logged_but_still_does_not_escape(self) -> None:
        """A serialization bug must not be able to skip a caller's cleanup
        either — it is loud in the log (`log.exception`) instead."""

        async def died() -> None:
            raise ValueError("a model that would not serialize")

        task = asyncio.create_task(died())
        await asyncio.sleep(0)
        await drain_pump(task, stream="test")

    async def test_a_live_pump_is_cancelled_and_consumed(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(forever())
        await started.wait()
        await drain_pump(task, stream="test")
        assert task.cancelled()
