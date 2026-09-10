"""Parameterised commands: the shape check that happens *before* the bus.

PR-E closes a wire that was laid and connected at neither end — ``params`` rode
the relay and the window dropped it, while the manifest hardcoded
``takes_params: false``. This suite covers the backend half of the new end: the
relay validates an incoming ``params`` against the schema the **window**
published, and refuses without publishing anything.

The load-bearing assertion in almost every test here is the **bus spy at zero
publishes**. A refusal that still reached the bus would be a refusal the window
also acted on, and "validated before the bus" would be a comment rather than a
property.

The other half — whether the argument is a *member* of the closed set (a layout
this window has, a folder on the recent list, a folder under the workspace root)
— is the window's, and lives in ``ui/src/commandRelay.test.ts`` and
``ui/e2e/cli-routine.spec.ts``.
"""

import asyncio
from typing import Any

from workbench_server.models.commands import (
    MAX_PARAM_CHARS,
    CommandInvokeEvent,
    CommandInvokeResult,
    CommandManifest,
    CommandManifestItem,
    CommandParamSpec,
    CommandParamsSchema,
)
from workbench_server.services.agent_tools import (
    MAX_DESCRIPTION_CHARS,
    RUN_COMMAND,
    RUN_COMMAND_LIST_MAX,
    handle_run_command,
)
from workbench_server.services.commands import CommandRelay
from workbench_server.services.event_bus import EventBus

LAYOUT_SWITCH = CommandManifestItem(
    id="layout.switch",
    title="Switch to a named layout",
    takes_params=True,
    params_schema=CommandParamsSchema(
        params=[CommandParamSpec(name="name", detail="a layout this window has")]
    ),
)
SESSION_START = CommandManifestItem(
    id="session.start",
    title="Start an agent session with a prompt",
    takes_params=True,
    params_schema=CommandParamsSchema(
        params=[
            CommandParamSpec(name="prompt", detail="what to ask"),
            CommandParamSpec(name="cwd", required=False, max_length=260, detail="a folder"),
        ]
    ),
)
PLAIN = CommandManifestItem(id="view.toggleTheme", title="Toggle theme")

MANIFEST = CommandManifest(commands=[PLAIN, LAYOUT_SWITCH, SESSION_START])


def _relay() -> tuple[CommandRelay, EventBus, asyncio.Queue[Any]]:
    """A relay with the manifest above and a subscriber watching the bus."""
    bus = EventBus()
    relay = CommandRelay(bus, timeout=0.05)
    relay.set_manifest(MANIFEST)
    return relay, bus, bus.subscribe()


async def _refused(command_id: str, params: dict[str, Any]) -> str:
    """Invoke, assert it was refused *without touching the bus*, return the why."""
    relay, _bus, queue = _relay()
    result = await relay.invoke(command_id, params)
    assert result.dispatched is False, "a refusal must not claim it reached a window"
    assert result.ok is False
    assert queue.empty(), "the refusal was published on the bus anyway"
    return result.detail


# --- the four refusals, each naming the field --------------------------------


async def test_an_unknown_field_is_refused_before_the_bus() -> None:
    detail = await _refused("layout.switch", {"nmae": "Review"})
    assert "no parameter 'nmae'" in detail
    assert "It takes: name" in detail


async def test_a_missing_required_field_is_refused_with_its_hint() -> None:
    detail = await _refused("session.start", {"cwd": "src"})
    assert "needs 'prompt'" in detail
    assert "what to ask" in detail


async def test_a_wrong_type_is_refused_naming_the_type_it_got() -> None:
    detail = await _refused("layout.switch", {"name": 7})
    assert "'name' must be a string, got int" in detail


async def test_a_value_past_the_cap_is_refused_naming_the_cap() -> None:
    detail = await _refused("session.start", {"prompt": "hi", "cwd": "x" * 261})
    assert "the limit is 260" in detail


async def test_a_field_with_no_cap_falls_back_to_the_shared_ceiling() -> None:
    detail = await _refused("session.start", {"prompt": "x" * (MAX_PARAM_CHARS + 1)})
    assert str(MAX_PARAM_CHARS) in detail


async def test_a_parameterless_command_sent_arguments_says_so() -> None:
    """Silently ignoring them would be the caller believing it asked for
    something it did not get — the AXI failure this repo names 'silence'."""
    detail = await _refused("view.toggleTheme", {"dark": "yes"})
    assert "takes no parameters" in detail
    assert "dark" in detail


# --- and the accepting path, which must reach the bus intact -----------------


async def test_valid_params_reach_the_window_verbatim() -> None:
    relay, _bus, queue = _relay()
    invoke = asyncio.ensure_future(relay.invoke("session.start", {"prompt": "hi", "cwd": "src"}))
    event = await asyncio.wait_for(queue.get(), 1.0)
    assert isinstance(event, CommandInvokeEvent)
    assert event.command_id == "session.start"
    assert event.params == {"prompt": "hi", "cwd": "src"}
    relay.resolve(event.invocation_id, ok=True, detail="Start an agent session with a prompt")
    result = await asyncio.wait_for(invoke, 1.0)
    assert result.ok is True


async def test_an_absent_optional_field_is_accepted() -> None:
    relay, _bus, queue = _relay()
    invoke = asyncio.ensure_future(relay.invoke("session.start", {"prompt": "hi"}))
    event = await asyncio.wait_for(queue.get(), 1.0)
    assert isinstance(event, CommandInvokeEvent)
    assert event.params == {"prompt": "hi"}
    relay.resolve(event.invocation_id, ok=True, detail="")
    await asyncio.wait_for(invoke, 1.0)


# --- the agent tool's listing: a hint, and the budget it has to fit ----------


class _Invoker:
    def __init__(self, manifest: CommandManifest) -> None:
        self._manifest = manifest

    def manifest(self) -> CommandManifest:
        return self._manifest

    def is_registered(self, command_id: str) -> bool:
        return any(item.id == command_id for item in self._manifest.commands)

    async def invoke(self, command_id: str, params: dict[str, Any]) -> CommandInvokeResult:
        return CommandInvokeResult(invocation_id="x", dispatched=True, ok=True, detail="")


def _text(result: dict[str, Any]) -> str:
    content = result["content"]
    assert isinstance(content, list)
    return str(content[0]["text"])


async def test_only_a_parameterised_command_carries_a_hint() -> None:
    text = _text(await handle_run_command(_Invoker(MANIFEST), {}))
    assert "layout.switch :: Switch to a named layout  {name:str}" in text
    # …the optional argument marked, so an agent does not have to try one to
    # find out (AXI shape 3, applied to a schema).
    assert "session.start :: Start an agent session with a prompt  {prompt:str,cwd:str?}" in text
    # …and the parameterless majority is exactly as it was: the manifest is one
    # of the biggest results this tool returns, and a schema per row would not
    # fit inside its budget.
    assert "view.toggleTheme :: Toggle theme\n" in text


async def test_a_realistic_manifest_with_hints_fits() -> None:
    """The budget re-measured, not re-asserted by clamping.

    ``clamp_result`` guarantees the assertion `<= max_result_bytes` trivially, so
    that alone would pass a listing that was silently cut in half. This builds
    the *real* shape — a full window's worth of commands at the cap, three of
    them parameterised — and asserts the whole thing arrived, then states what it
    actually measured.
    """
    plain = [
        CommandManifestItem(id=f"panel.some.tool.{i:02d}", title=f"Focus the {i:02d} panel")
        for i in range(RUN_COMMAND_LIST_MAX - 3)
    ]
    manifest = CommandManifest(
        commands=[
            *plain,
            LAYOUT_SWITCH,
            SESSION_START,
            CommandManifestItem(
                id="workspace.open",
                title="Open a folder as the workspace…",
                takes_params=True,
                params_schema=CommandParamsSchema(params=[CommandParamSpec(name="path")]),
            ),
        ]
    )
    text = _text(await handle_run_command(_Invoker(manifest), {}))
    size = len(text.encode())
    assert size <= RUN_COMMAND.max_result_bytes, size
    # Nothing was withheld: 50 rows in, 50 rows out, no capped-count footer.
    assert len(text.splitlines()) == RUN_COMMAND_LIST_MAX
    assert "more; these are the first" not in text
    # Measured 2,121 bytes of the 2,560 budget when this was written; the same
    # manifest with the three schemas stripped is 2,074, so the hints cost 47
    # bytes in total. Pinned loosely: what must not happen is the margin quietly
    # going to zero, not the number never moving.
    assert size < RUN_COMMAND.max_result_bytes - 100, size


def test_the_reworded_description_still_fits_the_shared_ceiling() -> None:
    """The description grew to name the hint. It is paid on every request of
    every session, so it is budgeted like everything else in this registry."""
    assert len(RUN_COMMAND.description) <= MAX_DESCRIPTION_CHARS
    # The input schema is untouched by PR-E — `params` was always there — so its
    # own ceiling must not have moved either.
    assert RUN_COMMAND.max_schema_bytes == 420
    assert RUN_COMMAND.schema_bytes <= RUN_COMMAND.max_schema_bytes


def test_the_hint_is_the_compact_shape_and_not_json_schema() -> None:
    schema = CommandParamsSchema(
        params=[
            CommandParamSpec(name="prompt"),
            CommandParamSpec(name="cwd", required=False),
        ]
    )
    assert schema.hint() == "{prompt:str,cwd:str?}"
    # The claim the budget rests on: the hint is an order of magnitude smaller
    # than the schema it stands for.
    assert len(schema.hint()) * 5 < len(schema.model_dump_json())
