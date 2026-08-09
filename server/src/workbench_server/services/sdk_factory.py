"""The real ClaudeSDKClient factory + the context-bridge MCP server.

Isolated here so nothing else imports the SDK — sessions stay testable with fakes.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import structlog

from workbench_server.config import Settings
from workbench_server.models.agents import SessionKind, UiState
from workbench_server.services.agent_sessions import SdkClient, SessionBridge
from workbench_server.services.agent_tools import (
    GET_WORKSPACE_STATE,
    LIST_WORKERS,
    MCP_SERVER_NAME,
    OFFICE_READ,
    OFFICE_RECONCILE,
    OFFICE_WRITE,
    PRESENT_PLAN,
    READ_WORKER,
    REPORT_FINDINGS,
    RUN_COMMAND,
    RUN_GATES,
    SEND_TO_WORKER,
    SPAWN_WORKER,
    STOP_WORKER,
    WORKSPACE_SEARCH,
    CommandInvoker,
    FindingsReceiver,
    OfficeDocumentAccess,
    OrchestratorHandle,
    ReconciliationRunner,
    WorkspaceSearcher,
    allowed_tool_names,
    handle_list_workers,
    handle_office_read,
    handle_office_reconcile,
    handle_office_write,
    handle_present_plan,
    handle_read_worker,
    handle_report_findings,
    handle_run_command,
    handle_run_gates,
    handle_send_to_worker,
    handle_spawn_worker,
    handle_stop_worker,
    handle_workspace_search,
    tools_for,
    workspace_state_result,
)
from workbench_server.services.permission_broker import build_pre_tool_use_hook
from workbench_server.services.skills_bundle import PLUGIN_NAME, bundled_plugin_path

log = structlog.get_logger()

# Tools the agent may use inside its folder without asking. Deliberately
# file-only, and deliberately the *whole* reason editing does not prompt: a
# whole-tool entry here auto-approves the tool before can_use_tool is consulted,
# which is exactly what we want for the four file tools provenance correlates.
#
# Everything else — shell, web — is absent, so it reaches a human. For the shell
# launchers that guarantee is enforced twice: absence from this list, *and* the
# PreToolUse broker in ``services/permission_broker.py``, which binds even when a
# permission mode or a workspace settings file would otherwise auto-approve.
_AUTO_ALLOWED = ["Read", "Edit", "Write", "Glob", "Grep"]

# The same list minus the two that write. **Gating this by kind is new code, not
# an addition**: until M6 PR 2 the list above was spread into every session's
# ``allowed_tools`` unconditionally, and a whole-tool entry there auto-approves
# *ahead of* ``can_use_tool``. A reviewer assembled by adding a branch somewhere
# else would therefore still have had ``Write`` and ``Edit`` pre-approved, with
# the deny-and-log path below never reached — read-only in the docstring and
# read-write on the disk.
_REVIEWER_AUTO_ALLOWED = ["Read", "Glob", "Grep"]

# …and the other half, which does a different job. ``allowed_tools`` decides what
# is *auto-approved*; per the SDK's own docstring only ``disallowed_tools``
# removes a tool "from the model's context" so it cannot be used at all. This
# codebase passed neither before now. Both are set for a reviewer on purpose:
# without the gate above the tool is silently pre-approved, and without this one
# the reviewer spends turns asking for a tool it is never going to get.
#
# ``PowerShell`` rides along with ``Bash`` for the reason
# ``permission_broker.BROKERED_TOOLS`` names both: on Windows — the platform this
# project targets first — the CLI exposes the shell as ``PowerShell``, so naming
# only ``Bash`` would leave the shell in a Windows reviewer's context.
_REVIEWER_DISALLOWED = ["Write", "Edit", "Bash", "PowerShell", "NotebookEdit"]

#: What a reviewer is told when it asks for a tool it does not have. It is a
#: finding it should make, not a permission it should wait for — said in the
#: denial so the turn continues instead of stalling on an appeal.
REVIEWER_DENY_MESSAGE = (
    "This is a read-only review session: it cannot write, edit or run commands, "
    "and it cannot ask a human for permission. If the change needs a tool you "
    "were not given in order to be verified, report that as a finding."
)


def auto_allowed_for(kind: SessionKind) -> list[str]:
    """The builtin tools this kind may use without a prompt.

    One function so the gate is in one place and testable without building an
    options object. Every kind but ``reviewer`` gets exactly what it always got.
    """
    return list(_REVIEWER_AUTO_ALLOWED if kind == "reviewer" else _AUTO_ALLOWED)


def disallowed_for(kind: SessionKind) -> list[str]:
    """Builtin tools removed from this kind's context entirely.

    Empty for every kind but ``reviewer`` — this tightens one kind and changes
    nothing for the others, which is what its test asserts.
    """
    return list(_REVIEWER_DISALLOWED) if kind == "reviewer" else []


def is_unattended(kind: SessionKind) -> bool:
    """Whether this kind runs with **no human watching**, and so may never prompt.

    A ``reviewer`` is commissioned by a check, not opened by a person: there is
    no chat window bound to it and nobody is waiting to answer a card. A
    permission request from one would either hang the validation that started it
    or interrupt a user who never asked for a reviewer — so it is answered here,
    with a denial and a log line, and never escalated.
    """
    return kind == "reviewer"


# The two bundled skills something already tells the agent to reach for on its
# own: the system-prompt append names plan-visual, and remember's own description
# has it read workspace memory at the start of unfamiliar work. Without these
# rules that guidance opens every plan — and every session — with a permission
# dialog, and a user who declines gets exactly the output the guidance existed to
# prevent. These are narrow ``Skill(name)`` specifiers, never a bare ``Skill``:
# only a whole-tool entry shadows can_use_tool, so every other skill — the user's
# own included, and our own workbench-dev — still reaches the UI prompt.
_AUTO_ALLOWED_SKILLS = [f"Skill({PLUGIN_NAME}:plan-visual)", f"Skill({PLUGIN_NAME}:remember)"]


class UiStateStore:
    """Latest UI state pushed by the frontend; read by agents via MCP."""

    def __init__(self) -> None:
        self.state = UiState()


def build_context_bridge(
    store: UiStateStore,
    bridge: SessionBridge,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    kind: SessionKind = "chat",
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
    findings: FindingsReceiver | None = None,
) -> Any:
    """In-process MCP server exposing the workbench tools to one session.

    Names, descriptions, input schemas and bodies all come from the tool
    registry (``services/agent_tools.py``); this function is only the SDK
    wiring, so adding a tool never means editing an allow-list here as well.

    **The toolset is a selection over** :func:`tools_for`, not a base list this
    function appends to. That distinction is the whole of M6 PR 2's isolation
    story and it is worth stating because the previous shape looked equivalent:
    until PR 2 this built a hardcoded list of every tool for *every* kind and
    only ever added the orchestrator's five to it, so no kind could **subtract**.
    ``tools_for("reviewer")`` returns ``report_findings`` alone, and a reviewer
    that still carried ``office_write`` and ``run_command`` because the
    construction never consulted that function would be read-only in name only.
    The per-kind exposed *names* are asserted in ``test_sdk_factory.py`` —
    names and not counts, because a count passes while a tool is swapped.

    A tool the model can *see* is a tool it will try, so a kind that cannot use
    one does not carry it: five "you are not an orchestrator" errors are five
    round trips plus five schemas paid on every request of every chat session,
    and for a reviewer the same argument is a safety one rather than a budget one.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool

    @tool(
        GET_WORKSPACE_STATE.name,
        GET_WORKSPACE_STATE.description,
        GET_WORKSPACE_STATE.input_schema,
    )
    async def get_workspace_state(args: dict[str, Any]) -> dict[str, Any]:
        return workspace_state_result(store.state)

    @tool(PRESENT_PLAN.name, PRESENT_PLAN.description, PRESENT_PLAN.input_schema)
    async def present_plan(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_present_plan(bridge, args)

    @tool(OFFICE_READ.name, OFFICE_READ.description, OFFICE_READ.input_schema)
    async def office_read(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_read(reader, args)

    @tool(OFFICE_WRITE.name, OFFICE_WRITE.description, OFFICE_WRITE.input_schema)
    async def office_write(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_write(reader, args)

    @tool(OFFICE_RECONCILE.name, OFFICE_RECONCILE.description, OFFICE_RECONCILE.input_schema)
    async def office_reconcile(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_office_reconcile(reconciler, args)

    @tool(RUN_COMMAND.name, RUN_COMMAND.description, RUN_COMMAND.input_schema)
    async def run_command(args: dict[str, Any]) -> dict[str, Any]:
        return await handle_run_command(commands, args)

    @tool(WORKSPACE_SEARCH.name, WORKSPACE_SEARCH.description, WORKSPACE_SEARCH.input_schema)
    async def workspace_search(args: dict[str, Any]) -> dict[str, Any]:
        return handle_workspace_search(searcher, args)

    @tool(RUN_GATES.name, RUN_GATES.description, RUN_GATES.input_schema)
    async def run_gates(args: dict[str, Any]) -> dict[str, Any]:
        # ``bridge.session_id`` and nothing from ``args``: the checkout the gates
        # run in is resolved from *this* session's id — the shipped
        # ``handle_spawn_worker(orchestrator, bridge.session_id, args)`` shape —
        # so the tool cannot be pointed at another session's slot.
        return await handle_run_gates(reconciler, bridge.session_id, args)

    @tool(REPORT_FINDINGS.name, REPORT_FINDINGS.description, REPORT_FINDINGS.input_schema)
    async def report_findings(args: dict[str, Any]) -> dict[str, Any]:
        # ``bridge.session_id`` again, and here it is load-bearing twice over:
        # it settles *this* reviewer's review and no other, and it is why the
        # tool needs no session argument a model could get wrong.
        return handle_report_findings(findings, bridge.session_id, args)

    # Name → body, for every tool this session *could* carry. The selection a few
    # lines down is what decides which ones it actually gets, and it reads off
    # the registry rather than off this dict — so a tool added to ``AGENT_TOOLS``
    # without a body here fails loudly at construction instead of vanishing from
    # a session that was supposed to have it.
    bodies: dict[str, Any] = {
        GET_WORKSPACE_STATE.name: get_workspace_state,
        PRESENT_PLAN.name: present_plan,
        OFFICE_READ.name: office_read,
        OFFICE_WRITE.name: office_write,
        OFFICE_RECONCILE.name: office_reconcile,
        RUN_COMMAND.name: run_command,
        WORKSPACE_SEARCH.name: workspace_search,
        # Carried through the PR 2 refactor deliberately, not incidentally: PR 1
        # appended ``run_gates`` to the very list this rewrite replaced, and the
        # plan named dropping it here as the hazard of doing these two lanes at
        # once — a green suite and a missing tool. ``test_sdk_factory.py`` asserts
        # it is in the chat and worker name sets.
        RUN_GATES.name: run_gates,
        REPORT_FINDINGS.name: report_findings,
    }
    if kind == "orchestrator" and orchestrator is not None:
        cost = session_cost or (lambda _worker_id: 0.0)

        @tool(SPAWN_WORKER.name, SPAWN_WORKER.description, SPAWN_WORKER.input_schema)
        async def spawn_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await handle_spawn_worker(orchestrator, bridge.session_id, args)

        @tool(LIST_WORKERS.name, LIST_WORKERS.description, LIST_WORKERS.input_schema)
        async def list_workers(args: dict[str, Any]) -> dict[str, Any]:
            return handle_list_workers(orchestrator, bridge.session_id, cost)

        @tool(READ_WORKER.name, READ_WORKER.description, READ_WORKER.input_schema)
        async def read_worker(args: dict[str, Any]) -> dict[str, Any]:
            return handle_read_worker(orchestrator, bridge.session_id, args)

        @tool(SEND_TO_WORKER.name, SEND_TO_WORKER.description, SEND_TO_WORKER.input_schema)
        async def send_to_worker(args: dict[str, Any]) -> dict[str, Any]:
            return handle_send_to_worker(orchestrator, bridge.session_id, args)

        @tool(STOP_WORKER.name, STOP_WORKER.description, STOP_WORKER.input_schema)
        async def stop_worker(args: dict[str, Any]) -> dict[str, Any]:
            return await handle_stop_worker(orchestrator, bridge.session_id, args)

        bodies.update(
            {
                SPAWN_WORKER.name: spawn_worker,
                LIST_WORKERS.name: list_workers,
                READ_WORKER.name: read_worker,
                SEND_TO_WORKER.name: send_to_worker,
                STOP_WORKER.name: stop_worker,
            }
        )

    # The selection. A spec with no body is skipped rather than crashing the
    # session — the only way that happens today is an ``orchestrator`` kind wired
    # without an orchestrator handle, which is a mis-wiring worth a log line and
    # not worth taking a user's session down for.
    tools: list[Any] = []
    for spec in tools_for(kind):
        body = bodies.get(spec.name)
        if body is None:
            log.warning("sdk_factory.tool_body_missing", tool=spec.name, kind=kind)
            continue
        tools.append(body)

    return create_sdk_mcp_server(name=MCP_SERVER_NAME, version="1.0.0", tools=tools)


def build_agent_options(
    store: UiStateStore,
    settings: Settings,
    folder: Path,
    resume_session_id: str | None,
    bridge: SessionBridge,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    kind: SessionKind = "chat",
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
    findings: FindingsReceiver | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
) -> Any:
    """The single construction point for ``ClaudeAgentOptions``.

    Split out of the factory closure so the whole configuration — permissions,
    the context bridge, the bundled plugin, which filesystem settings a session
    inherits — is assertable in tests without connecting a client.

    Skills are *not* enabled through ``options.skills``: ``"all"`` appends a bare
    ``Skill`` entry to ``allowed_tools``, which shadows ``can_use_tool`` and
    silently auto-allows every discovered skill, and a list would double as a
    context filter that hides the user's own project skills. The plugin is
    passed on its own instead, so bundled skills are simply *available* under
    the ``workbench:`` prefix; only the two named in ``_AUTO_ALLOWED_SKILLS``
    carry a narrow allow rule, and every other invocation still reaches the UI's
    permission prompt like any other non-file tool.
    """
    from claude_agent_sdk import (
        ClaudeAgentOptions,
        HookMatcher,
        PermissionResultAllow,
        PermissionResultDeny,
        SdkPluginConfig,
        SettingSource,
    )

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        # The unattended short-circuit — **new safety-critical code**, with no
        # precedent in this repo to copy. Until M6 PR 2 this callback escalated
        # for every kind unconditionally, ``worker`` and ``orchestrator``
        # included. A reviewer is commissioned by a validation run, not opened by
        # a person, so an escalation from one has nowhere good to go: it either
        # blocks the check for the full prompt timeout or interrupts a user who
        # never asked for a reviewer.
        #
        # It returns **before** awaiting ``bridge.ask_permission``, and that
        # ordering is the feature rather than an implementation detail — "it is
        # denied" and "it is denied without waking the user" are different claims,
        # and only the second one is what this buys. ``test_sdk_factory.py``
        # asserts it with a spy counting awaits, not by reading this branch.
        if is_unattended(kind):
            log.info(
                "agent.unattended_tool_denied",
                session=bridge.session_id,
                kind=kind,
                tool=tool_name,
            )
            return PermissionResultDeny(message=REVIEWER_DENY_MESSAGE)
        if await bridge.ask_permission(tool_name, tool_input):
            return PermissionResultAllow()
        return PermissionResultDeny(message="User declined in the workbench UI")

    plugins: list[SdkPluginConfig] = []
    if settings.bundled_skills:
        plugin_path = bundled_plugin_path()
        if plugin_path is not None:
            plugins.append(SdkPluginConfig(type="local", path=str(plugin_path)))

    # ``None`` is the SDK's implicit default: load every filesystem settings
    # source, including the user's global ~/.claude — its hooks and permission
    # rules, not only its skills. ``["project", "local"]`` keeps everything the
    # *workspace* configures: CLAUDE.md and .claude/settings.json, plus the
    # machine-local .claude/settings.local.json where Claude Code records
    # "always allow" rules, local hooks and local MCP servers — so a folder
    # behaves the same in a Workbench session as it does in plain Claude Code.
    # Only the global machine scope is dropped.
    setting_sources: list[SettingSource] | None = (
        None if settings.inherit_user_settings else ["project", "local"]
    )

    return ClaudeAgentOptions(
        cwd=str(folder),
        resume=resume_session_id,
        allowed_tools=[
            # Never widened for a worker, and that is the whole permission story
            # of Mission Control: no shell launcher is here, so a worker's shell
            # request blocks and escalates to the board for a human to answer.
            # An orchestrator adds its own five tools below and nothing else —
            # it gains no way to run anything. **Narrowed** for a reviewer, which
            # is the direction this list had never gone before M6 PR 2: see
            # ``auto_allowed_for``.
            *auto_allowed_for(kind),
            *(_AUTO_ALLOWED_SKILLS if plugins else []),
            *allowed_tool_names(kind),
        ],
        # The other half of the reviewer's isolation, and empty for every other
        # kind. ``allowed_tools`` above stops a tool being *pre-approved*; only
        # this takes it out of the model's context, so the reviewer neither has
        # the tool nor spends turns asking for it. Passing neither — which is
        # what this server did until now — is what made the kind-gate above
        # insufficient on its own.
        disallowed_tools=disallowed_for(kind),
        # NOT ``acceptEdits``. That mode auto-approves a Bash call whose base
        # command is one of ``mkdir touch rm rmdir mv cp sed`` inside cwd, and an
        # auto-approved call never reaches ``can_use_tool`` — so ``rm -rf`` and
        # ``sed -i`` in the workspace used to run with no permission card, no
        # provenance claim and no activity row. Measured, not inferred; the
        # regression test is ``test_permission_broker.py``.
        #
        # Dropping it costs nothing: the four file tools are auto-approved by
        # ``allowed_tools`` above, which shadows ``can_use_tool`` on its own, so
        # ordinary Write/Edit still does not prompt (also measured). ``default``
        # is therefore the *same* editing ergonomics minus the shell fast path.
        permission_mode="default",
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        # The broker that binds regardless of mode. It is not redundant with
        # ``can_use_tool``: a hook is dispatched for every tool call, while the
        # callback is only reached for calls the CLI resolves to "ask" — so a
        # ``permissions.allow`` rule in a folder the user did not write, or a
        # mode a subagent inherited, cannot walk around this one. Nor does the
        # pair cost two prompts: both resolve through the same
        # ``bridge.ask_permission`` future, and the hook's explicit allow
        # suppresses the callback — asserted against the real CLI by
        # ``TestTheEndToEndRepro`` in ``server/tests/test_permission_broker.py``,
        # which counts the awaits for a single approved shell call.
        # ``cast`` because the broker is deliberately SDK-free (this module is
        # the only one that imports ``claude_agent_sdk`` at all), so it types its
        # hook input as a plain dict rather than the SDK's TypedDict union. The
        # keys it reads — ``tool_name``, ``tool_input``, ``agent_id`` — are the
        # ones ``PreToolUseHookInput`` declares.
        hooks={
            "PreToolUse": [
                HookMatcher(
                    hooks=[
                        cast(
                            "Any",
                            # ``kind`` is passed because there are **two** paths to
                            # the user's screen, not one: this hook escalates
                            # brokered shell calls through the same future
                            # ``can_use_tool`` uses, so short-circuiting only the
                            # callback would leave a reviewer's ``Bash`` request
                            # still able to raise a card. Both, or neither.
                            build_pre_tool_use_hook(bridge.ask_permission, bridge.session_id, kind),
                        )
                    ]
                )
            ]
        },
        mcp_servers={
            MCP_SERVER_NAME: build_context_bridge(
                store,
                bridge,
                reader,
                commands,
                reconciler,
                searcher,
                kind,
                orchestrator,
                session_cost,
                findings,
            )
        },
        plugins=plugins,
        setting_sources=setting_sources,
        # Both unused in this codebase until M6 PR 2, and both set only where a
        # ceiling is the difference between a feature and a liability: a review
        # runs unattended and spends the user's money, so it gets a turn cap and
        # a dollar cap. ``None`` for every other kind leaves the SDK's own
        # defaults exactly as they were.
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _system_append(kind),
        },
    )


#: What every ordinary session is told about the window it is running in.
_WORKBENCH_APPEND = (
    "You are running inside Workbench. Before editing files, call "
    "get_workspace_state and avoid editing files listed as dirty "
    "(unsaved user changes). When you propose multi-step work or "
    "alternatives, call present_plan instead of writing the plan "
    "as prose — the user answers it as an interactive card. Use the "
    "bundled workbench:plan-visual skill when authoring that card."
)

#: …and what a reviewer is told instead. Naming ``get_workspace_state`` and
#: ``present_plan`` to a session that has neither would cost it a turn to
#: discover they are not there, and the sentence about editing files is the
#: opposite of this session's job. The *review brief* is not here — it is the
#: prompt ``services/review.py`` sends, so the instruction and the diff it is
#: about arrive together.
_REVIEWER_APPEND = (
    "You are a review session inside Workbench. You are read-only: you cannot "
    "write, edit or run commands, and no human is watching this session, so "
    "nothing you ask permission for will be answered. Report what you find by "
    "calling report_findings exactly once. You do not approve or reject the "
    "change — a human reads your findings and decides."
)


def _system_append(kind: SessionKind) -> str:
    """The system-prompt append for this kind of session."""
    return _REVIEWER_APPEND if kind == "reviewer" else _WORKBENCH_APPEND


def sdk_client_factory(
    store: UiStateStore,
    reader: OfficeDocumentAccess,
    commands: CommandInvoker,
    reconciler: ReconciliationRunner,
    searcher: WorkspaceSearcher,
    settings: Settings | None = None,
    orchestrator: OrchestratorHandle | None = None,
    session_cost: Callable[[str], float] | None = None,
    findings: FindingsReceiver | None = None,
    reviewer_caps: Callable[[], tuple[int, float]] | None = None,
) -> Any:
    """Returns a ClientFactory closure for SessionManager.

    ``reader`` is the office-host service, narrowed to :class:`OfficeDocumentAccess`
    so a session can read *and* edit the live docked document without this module
    importing the service — the same one-way dependency the rest of the tools keep.
    ``commands`` is the command relay, narrowed to :class:`CommandInvoker`, so a
    session can invoke a registered window command the same way. ``reconciler`` is
    the validation service, narrowed to :class:`ReconciliationRunner`, so a session
    can run the reconciliation gate over a workbook it just wrote. ``findings`` is
    the adversarial-review check, narrowed to :class:`FindingsReceiver`, so a
    reviewer session's ``report_findings`` call settles the review that
    commissioned it.

    ``reviewer_caps`` is read **per session** rather than captured once, because
    a review may name its own ceilings in its ``ValidationSpec.params`` and the
    check parks them there just before it spawns; a value baked in at server
    construction would silently ignore the spec.
    """
    resolved = settings or Settings()

    def factory(
        folder: Path,
        resume_session_id: str | None,
        bridge: SessionBridge,
        kind: SessionKind = "chat",
    ) -> SdkClient:
        from claude_agent_sdk import ClaudeSDKClient

        turns: int | None = None
        budget: float | None = None
        if kind == "reviewer" and reviewer_caps is not None:
            turns, budget = reviewer_caps()

        options = build_agent_options(
            store,
            resolved,
            folder,
            resume_session_id,
            bridge,
            reader,
            commands,
            reconciler,
            searcher,
            kind,
            orchestrator,
            session_cost,
            findings,
            turns,
            budget,
        )
        client: SdkClient = ClaudeSDKClient(options=options)
        return client

    return factory
