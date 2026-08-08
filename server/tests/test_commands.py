"""The command relay: manifest, invoke, result — and the two safety constraints.

Item 14's exit line is "a command registered today is invocable from a shell and
from an agent, and a request without the token is refused". These cover the
backend half of both: the relay validates against the published manifest and
refuses the rest (only *registered* commands), and the endpoint is gated by the
same local-auth middleware everything under ``/api/`` is (the token). The UI
executor and the CLI have their own suites (vitest, and the live E2E).
"""

import asyncio

from httpx import ASGITransport, AsyncClient

from workbench_server.config import Settings
from workbench_server.main import create_app
from workbench_server.models.commands import (
    CommandInvokeEvent,
    CommandInvokeResult,
    CommandManifest,
    CommandManifestItem,
)
from workbench_server.services.agent_tools import RUN_COMMAND, handle_run_command
from workbench_server.services.commands import CommandRelay
from workbench_server.services.event_bus import EventBus

MANIFEST = CommandManifest(
    commands=[
        CommandManifestItem(id="view.toggleTheme", title="Toggle theme"),
        CommandManifestItem(id="panel.terminal", title="Focus Terminal panel"),
    ]
)


# --- the service, in isolation ----------------------------------------------


async def test_invoke_publishes_an_event_and_a_result_completes_it() -> None:
    """The relay is the event bus backwards: invoke publishes a typed event, and
    the window's result is what lets the awaiting caller return."""
    bus = EventBus()
    relay = CommandRelay(bus)
    relay.set_manifest(MANIFEST)
    queue = bus.subscribe()

    invoke = asyncio.ensure_future(relay.invoke("view.toggleTheme", {}))
    event = await asyncio.wait_for(queue.get(), 1.0)
    assert isinstance(event, CommandInvokeEvent)
    assert event.command_id == "view.toggleTheme"

    assert relay.resolve(event.invocation_id, ok=True, detail="Toggle theme") is True
    result = await asyncio.wait_for(invoke, 1.0)
    assert result.dispatched is True
    assert result.ok is True
    assert result.detail == "Toggle theme"


async def test_invoke_without_a_connected_window_is_refused_not_hung() -> None:
    """No manifest means no window: the honest answer is 'connect one', returned
    at once, not a timeout (AXI shape 2)."""
    relay = CommandRelay(EventBus())
    result = await relay.invoke("view.toggleTheme", {})
    assert result.dispatched is False
    assert result.ok is False
    assert "window" in result.detail.lower()


async def test_invoke_times_out_when_the_window_never_answers() -> None:
    """A window that received the event but never reports back cannot wedge the
    caller — the invoke is bounded by its timeout."""
    relay = CommandRelay(EventBus(), timeout=0.05)
    relay.set_manifest(MANIFEST)
    result = await relay.invoke("view.toggleTheme", {})
    assert result.dispatched is True
    assert result.ok is False
    assert "confirm" in result.detail.lower()


def test_a_stale_result_is_a_no_op() -> None:
    """A result for an invocation that already timed out (or never existed) is
    dropped, not an error — the window may post after the caller gave up."""
    relay = CommandRelay(EventBus())
    assert relay.resolve("no-such-invocation", ok=True, detail="") is False


def test_only_published_ids_are_registered() -> None:
    relay = CommandRelay(EventBus())
    assert relay.is_registered("view.toggleTheme") is False
    relay.set_manifest(MANIFEST)
    assert relay.is_registered("view.toggleTheme") is True
    assert relay.is_registered("workspace.open") is False


# --- through the REST surface ------------------------------------------------


async def test_get_commands_lists_the_published_manifest(settings: Settings) -> None:
    app = create_app(settings)
    relay: CommandRelay = app.state.commands
    relay.set_manifest(MANIFEST)
    token = app.state.auth_token
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Workbench-Token": token},
    ) as client:
        resp = await client.get("/api/commands")
        assert resp.status_code == 200
        ids = [item["id"] for item in resp.json()["commands"]]
        assert ids == ["view.toggleTheme", "panel.terminal"]


async def test_publish_then_invoke_relays_the_event_on_the_bus(settings: Settings) -> None:
    """The window PUTs its manifest, then a POST /invoke reaches a subscriber as a
    CommandInvokeEvent — the whole relay path end to end."""
    app = create_app(settings)
    bus: EventBus = app.state.event_bus
    token = app.state.auth_token
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Workbench-Token": token},
    ) as client:
        put = await client.put("/api/commands/manifest", json=MANIFEST.model_dump())
        assert put.status_code == 200

        queue = bus.subscribe()
        invoke = asyncio.ensure_future(
            client.post("/api/commands/invoke", json={"command_id": "panel.terminal"})
        )
        event = await asyncio.wait_for(queue.get(), 2.0)
        assert isinstance(event, CommandInvokeEvent)
        assert event.command_id == "panel.terminal"

        # Stand in for the window: report the result back over the same surface.
        report = await client.post(
            "/api/commands/result",
            json={"invocation_id": event.invocation_id, "ok": True, "detail": "Focus Terminal"},
        )
        assert report.status_code == 200

        resp = await asyncio.wait_for(invoke, 2.0)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["detail"] == "Focus Terminal"


async def test_an_unknown_id_is_a_404_not_a_silent_pass(settings: Settings) -> None:
    app = create_app(settings)
    app.state.commands.set_manifest(MANIFEST)
    token = app.state.auth_token
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Workbench-Token": token},
    ) as client:
        resp = await client.post("/api/commands/invoke", json={"command_id": "rm.-rf"})
        assert resp.status_code == 404
        assert "not a registered command" in resp.json()["detail"]


async def test_invoke_without_the_token_is_refused(settings: Settings) -> None:
    """Item 14's second constraint, and item 8's enforcement doing its job: with
    the middleware on, a tokenless invoke never reaches the router."""
    enforced = settings.model_copy(update={"enforce_auth": True, "auth_token": "the-token"})
    app = create_app(enforced)
    app.state.commands.set_manifest(MANIFEST)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/commands/invoke", json={"command_id": "view.toggleTheme"})
        assert resp.status_code == 403
        # The same request with the token gets *past* the gate — an unregistered
        # id so it 404s at once rather than waiting on a window that never
        # answers; either way it is not the auth 403, which is the point.
        ok = await client.post(
            "/api/commands/invoke",
            json={"command_id": "no.such.command"},
            headers={"X-Workbench-Token": "the-token"},
        )
        assert ok.status_code == 404


# --- the run_command agent tool ----------------------------------------------


class _Invoker:
    """CommandInvoker stub: a fixed manifest and a canned invoke outcome."""

    def __init__(self, manifest: CommandManifest, outcome: CommandInvokeResult) -> None:
        self._manifest = manifest
        self._outcome = outcome
        self.invoked: tuple[str, dict[str, object]] | None = None

    def manifest(self) -> CommandManifest:
        return self._manifest

    def is_registered(self, command_id: str) -> bool:
        return any(item.id == command_id for item in self._manifest.commands)

    async def invoke(self, command_id: str, params: dict[str, object]) -> CommandInvokeResult:
        self.invoked = (command_id, params)
        return self._outcome


def _result_text(result: dict[str, object]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    text = content[0]["text"]
    assert isinstance(text, str)
    return text


async def test_run_command_lists_the_manifest_with_no_id() -> None:
    invoker = _Invoker(MANIFEST, CommandInvokeResult(invocation_id="x", dispatched=False, ok=False))
    result = await handle_run_command(invoker, {})
    text = _result_text(result)
    assert "view.toggleTheme :: Toggle theme" in text
    assert invoker.invoked is None  # discovery does not run anything


async def test_run_command_says_none_when_no_window_is_connected() -> None:
    invoker = _Invoker(
        CommandManifest(), CommandInvokeResult(invocation_id="x", dispatched=False, ok=False)
    )
    text = _result_text(await handle_run_command(invoker, {}))
    assert "No commands available" in text


async def test_run_command_runs_a_registered_id() -> None:
    invoker = _Invoker(
        MANIFEST,
        CommandInvokeResult(invocation_id="x", dispatched=True, ok=True, detail="Toggle theme"),
    )
    result = await handle_run_command(invoker, {"command_id": "view.toggleTheme"})
    assert "is_error" not in result
    assert "ran view.toggleTheme" in _result_text(result)
    assert invoker.invoked == ("view.toggleTheme", {})


async def test_run_command_refuses_an_unregistered_id_without_invoking() -> None:
    invoker = _Invoker(MANIFEST, CommandInvokeResult(invocation_id="x", dispatched=False, ok=False))
    result = await handle_run_command(invoker, {"command_id": "rm.-rf"})
    assert result["is_error"] is True
    assert invoker.invoked is None


async def test_run_command_surfaces_a_failed_run_as_an_error() -> None:
    invoker = _Invoker(
        MANIFEST,
        CommandInvokeResult(
            invocation_id="x", dispatched=True, ok=False, detail="not available right now"
        ),
    )
    result = await handle_run_command(invoker, {"command_id": "panel.terminal"})
    assert result["is_error"] is True
    assert "did not run panel.terminal" in _result_text(result)


async def test_run_command_list_stays_within_its_result_budget() -> None:
    """A window with more commands than the list cap: the answer is bounded and
    says how many it withheld (AXI shape 1)."""
    big = CommandManifest(
        commands=[
            CommandManifestItem(id=f"tool.command.number-{n}", title=f"Do the {n}th thing")
            for n in range(120)
        ]
    )
    invoker = _Invoker(big, CommandInvokeResult(invocation_id="x", dispatched=False, ok=False))
    text = _result_text(await handle_run_command(invoker, {}))
    assert len(text.encode()) <= RUN_COMMAND.max_result_bytes
    assert "more; these are the first" in text
