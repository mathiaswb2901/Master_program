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

from typing import Any, Literal

from pydantic import BaseModel, Field

#: Ceiling on one parameter value, in characters, when the declaring command
#: names no tighter one. Long enough for a prompt worth sending, short enough
#: that a runaway argument is a refusal rather than a payload.
MAX_PARAM_CHARS = 4_000

#: Ceiling on how many parameters one command may declare. The three shipped
#: commands take one or two; this bounds the manifest a window can publish, so
#: a malformed registry cannot make ``GET /api/commands`` unbounded.
MAX_PARAMS_PER_COMMAND = 8


class CommandParamSpec(BaseModel):
    """One argument a parameterised command takes.

    **Strings only, deliberately.** Every argument space in the shipped set is a
    name, a path or a prompt, and a single type is what keeps both the published
    schema and the refusal messages small enough to be free — the window's whole
    manifest rides inside ``run_command``'s result budget. A command that one day
    needs a number states so by widening this union, in one place, with the
    budget re-measured.
    """

    name: str
    type: Literal["string"] = "string"
    required: bool = True
    #: Cap on the value's length. ``None`` means :data:`MAX_PARAM_CHARS`.
    max_length: int | None = Field(default=None, gt=0, le=MAX_PARAM_CHARS)
    #: A few words for the caller — what a valid value looks like. Kept short:
    #: the manifest is read by an agent on every discovery call.
    detail: str = ""

    def limit(self) -> int:
        return self.max_length or MAX_PARAM_CHARS


class CommandParamsSchema(BaseModel):
    """The whole argument shape of one parameterised command.

    Published *by the window*, because the window owns the registry — the relay
    validates against what it was told rather than holding a second opinion
    about what any command means (see ``services/commands.py``).
    """

    params: list[CommandParamSpec] = Field(default_factory=list, max_length=MAX_PARAMS_PER_COMMAND)

    def hint(self) -> str:
        """A compact one-line shape, for an agent's discovery listing.

        ``{name:str}``, ``{prompt:str,cwd:str?}`` — a trailing ``?`` marks an
        optional argument. JSON Schema would be truthful and roughly ten times
        the bytes, and this listing is paid on every discovery call.
        """
        inner = ",".join(f"{p.name}:str{'' if p.required else '?'}" for p in self.params)
        return "{" + inner + "}"


class CommandManifestItem(BaseModel):
    """One command the window will run on request.

    ``takes_params`` says whether this id expects a ``params`` object, so a CLI
    or agent can tell without guessing; ``params_schema`` says *what* — the
    closed argument shape the relay validates an invoke against before it ever
    reaches the bus. Both are absent/false for the parameterless majority, which
    is what keeps the published manifest small.
    """

    id: str
    title: str
    takes_params: bool = False
    params_schema: CommandParamsSchema | None = None


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
    #: Arguments for a parameterised command. Validated against the *published*
    #: ``params_schema`` before the request reaches the bus, so an unknown field
    #: or a missing argument is a typed refusal naming the field rather than a
    #: ten-second wait for a window that quietly did nothing. A command that
    #: published no schema takes no arguments, and says so when sent any.
    params: dict[str, Any] = Field(default_factory=dict)


class CommandInvokeEvent(BaseModel):
    """Pushed on ``/ws/events`` so the connected window runs one command.

    ``invocation_id`` correlates the window's ``POST /api/commands/result`` back
    to the request that is still awaiting it, so the caller (CLI or agent) hears
    whether the command actually ran rather than only that it was dispatched.
    """

    type: Literal["command_invoke"] = "command_invoke"
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
    is connected — or because its ``params`` did not match the published schema —
    comes back ``dispatched=False`` with the reason in ``detail``.
    """

    invocation_id: str
    dispatched: bool
    ok: bool
    detail: str = ""


# ---- the batch mode (`workbench-cmd --script`) ------------------------------
#
# A twelve-step morning routine is twelve interpreter starts, and after the
# import fix in `workbench_server.runtime` each one still costs ~0.7 s here
# (httpx's lazy httpcore/h11 machinery is most of what is left). `--script` runs
# the whole list in one process over one connection: one startup plus N relay
# round trips, each of which `services/commands.py` bounds at
# INVOKE_TIMEOUT_SECONDS. A persistent channel was measured against this and
# rejected — see docs/plan/productivity-loops.md §5.
#
# These are the CLI's own document types rather than wire payloads; they live
# here so the one place that describes what a command invocation *is* describes
# a batch of them too.

#: Ops in one script. Well past any routine a person writes by hand, and a
#: bound on how long a single `--script` run can hold the window.
MAX_SCRIPT_OPS = 64


class ScriptOp(BaseModel):
    """One step of a ``--script`` document: a command id and its arguments."""

    command_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class CommandScript(BaseModel):
    """A ``--script`` document: ``{"ops": [{"command_id": …, "params": …}]}``.

    An empty list is legal and says so out loud when it runs (AXI shape 2) —
    a script that silently did nothing is indistinguishable from one that broke.
    """

    ops: list[ScriptOp] = Field(default_factory=list, max_length=MAX_SCRIPT_OPS)


class ScriptResult(BaseModel):
    """How one op of a script went, for the line the CLI prints about it.

    ``index`` is 1-based because it is read by a person looking at a file, and
    line 0 of a routine is not a thing anybody counts.
    """

    index: int
    command_id: str
    ok: bool
    detail: str = ""

    def line(self, total: int) -> str:
        return f"{self.index}/{total} {'ok    ' if self.ok else 'FAILED'} {self.command_id}" + (
            f": {self.detail}" if self.detail else ""
        )
