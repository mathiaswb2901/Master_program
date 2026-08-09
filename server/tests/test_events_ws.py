"""`/ws/events` — the fan-out socket, and the disconnect it has to hear.

The endpoint sends and never receives anything meaningful, which is exactly why
it needs a receive loop: an ASGI server delivers `websocket.disconnect` to a
receive call and to nothing else. A handler without one is not merely untidy —
it never returns, so the subscription it holds lives as long as the process and
uvicorn cannot complete a graceful shutdown while it is outstanding.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.services.event_bus import EventBus


@pytest.mark.timeout(60)
def test_a_disconnected_client_stops_being_a_subscriber(settings: Settings, tmp_path: Path) -> None:
    """The leak in its own right: the bus must not keep fanning out to a ghost.

    Every publish walks the subscriber set, and a dropped queue is filled to its
    1000-event cap and then churned forever. One stale entry per socket the user
    ever opened is the cost of a handler that cannot tell it has been left.
    """
    app = create_app(settings)
    with TestClient(app) as client:
        bus: EventBus = app.state.event_bus
        # In-process subscribers (shortcuts, provenance, activity) hold queues of
        # their own for the life of the app, so the socket's is measured as a
        # delta against them rather than counted from zero.
        baseline = set(bus._subscribers)
        with client.websocket_connect("/ws/events") as ws:
            assert set(bus._subscribers) - baseline != set(), "the socket never subscribed"
            (tmp_path / "model.py").write_bytes(b"VERSION = 1\n")
            assert json.loads(ws.receive_text())["path"] == "model.py"
        # The drop happens on the server's task, not the client's, so give the
        # app's loop turns to run rather than assuming it already has.
        for _ in range(100):
            if set(bus._subscribers) == baseline:
                break
            client.get("/api/health")
        assert set(bus._subscribers) == baseline, "the socket closed and the subscription stayed"


@pytest.mark.timeout(60)
def test_two_clients_are_independent(settings: Settings, tmp_path: Path) -> None:
    """One socket closing must not take the other's subscription with it."""
    app = create_app(settings)
    with TestClient(app) as client:
        bus: EventBus = app.state.event_bus
        baseline = set(bus._subscribers)
        with client.websocket_connect("/ws/events") as first:
            with client.websocket_connect("/ws/events") as second:
                assert len(set(bus._subscribers) - baseline) == 2
                (tmp_path / "model.py").write_bytes(b"VERSION = 1\n")
                assert json.loads(first.receive_text())["path"] == "model.py"
                assert json.loads(second.receive_text())["path"] == "model.py"
            # `second` is gone; `first` still works.
            (tmp_path / "other.py").write_bytes(b"VERSION = 2\n")
            assert json.loads(first.receive_text())["path"] == "other.py"
