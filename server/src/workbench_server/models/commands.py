"""Command-relay schemas: invoking a window command from outside the window.

The window owns the command *registry* — every capability declares its commands
there (``ui/src/registry.ts``). The backend cannot run one directly, so this is
the seam that lets a shell or an agent reach one: the UI publishes the list of
invocable commands (its *manifest*) on connect, the backend validates an
incoming id against that list, and a ``CommandInvokeEvent`` on ``/ws/events``
carries the request back to the connected window — the event bus run in reverse
(backend -> client). The safety property is here in the types: only an id the
window published is invocable, and the invoke request is rejected before it ever
reaches the bus otherwise.
"""

from typing import Any

from pydantic import BaseModel, Field


class CommandManifestItem(BaseModel):
    """One command the window will run on request.

    ``takes_params`` is advisory — today's commands run parameterless, so it is
    ``False`` for all of them; it is here so a parameterised command can say so
    without a schema change, and so the CLI/agent can tell which ids expect a
    ``params`` object rather than guessing.
    """

    id: str
    title: str
    takes_params: bool = False


class CommandManifest(BaseModel):
    """What ``GET /api/commands`` returns and what the UI ``PUT``s on connect.

    Empty is the honest first state: no window has published yet. A CLI or agent
    that gets an empty manifest is told a window is not connected rather than
    left to read blankness (CLAUDE.md, AXI shape 2).
    """

    commands: list[CommandManifestItem] = Field(default_factory=list)


class CommandInvokeRequest(BaseModel):
    """``POST /api/commands/invoke`` — run this registered command."""

    command_id: str
    #: Forwarded to the window verbatim. Reserved: current commands ignore it.
    params: dict[str, Any] = Field(default_factory=dict)


class CommandInvokeEvent(BaseModel):
    """Pushed on ``/ws/events`` so the connected window runs one command.

    ``invocation_id`` correlates the window's ``POST /api/commands/result`` back
    to the request that is still awaiting it, so the caller (CLI or agent) hears
    whether the command actually ran rather than only that it was dispatched.
    """

    type: str = "command_invoke"
    invocation_id: str
    command_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class CommandResultRequest(BaseModel):
    """``POST /api/commands/result`` — the window reports how one invocation went.

    The window is the only authority on whether a command ran: it holds the
    registry and the dispatch. ``ok`` is that verdict; ``detail`` is a short
    human sentence (what ran, or why it did not).
    """

    invocation_id: str
    ok: bool
    detail: str = ""


class CommandInvokeResult(BaseModel):
    """The answer ``POST /api/commands/invoke`` returns to the caller.

    ``dispatched`` is whether the request reached a window at all; ``ok`` is
    whether that window then ran the command. A request refused because no window
    is connected comes back ``dispatched=False`` with the reason in ``detail``.
    """

    invocation_id: str
    dispatched: bool
    ok: bool
    detail: str = ""
