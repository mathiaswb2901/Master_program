import type { DockviewPanelApi, IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import { dockApiHandle, focusPanel } from "../dock";
import { paneId, paneInstance, parsePaneId } from "../panes";
import type { WorkbenchTool } from "../registry";
import { relativeTime } from "../relativeTime";
import { useStore } from "../store";
import type { SessionInfo, SessionState } from "../types";
import { Chat, statusVisual, TranscriptView } from "./Chat";
import { revealPane } from "./Panes";
import { planCommands, planShortcuts } from "./PlanCard";

function SessionRow({ session }: { session: SessionInfo }) {
  const state = useStore((s) => s.sessionStates[session.session_id] ?? session.state);
  const flags = useStore((s) => s.sessionFlags[session.session_id]);
  const selected = useStore(
    (s) =>
      s.activeSessionId === session.session_id ||
      s.transcriptView?.session.session_id === session.session_id,
  );
  const v = statusVisual(state, flags);
  return (
    <button
      type="button"
      className={"wb-session-row" + (selected ? " is-selected" : "")}
      onClick={() => useStore.getState().openSession(session)}
      title={session.title}
    >
      {session.live ? (
        <span
          className={"wb-dot" + (v.pulse ? " u-agent-pulse" : "")}
          style={{ background: v.color }}
          role="img"
          aria-label={v.label}
        />
      ) : (
        <span className="wb-dot wb-dot-disk" aria-hidden="true" />
      )}
      <span className="wb-session-title u-truncate">{session.title}</span>
      <span className="wb-session-time u-tabular">{relativeTime(session.updated_at)}</span>
    </button>
  );
}

/** Folder a new session binds to: the active file's directory, else the root. */
function activeFolder(): string {
  const path = useStore.getState().activePath;
  return path !== null && path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
}

/** Every session, newest first — live ones and resumable transcripts alike. */
function recentSessions(): SessionInfo[] {
  return useStore
    .getState()
    .folders.flatMap((group) => group.sessions)
    .sort((a, b) => b.updated_at - a.updated_at);
}

/** The ones a pane can be bound to: a pane is somewhere you work. */
const liveSessions = (): SessionInfo[] => recentSessions().filter((session) => session.live);

/** The session the detach command acts on: the focused agent *pane* if there is
 * one, else the browser's active chat. Null when neither names a session. */
function detachTargetSessionId(): string | null {
  const dock = dockApiHandle();
  const panelId = dock?.activeGroup?.activePanel?.id ?? dock?.activePanel?.id;
  if (panelId !== undefined) {
    const { toolId, instance } = parsePaneId(panelId);
    if (toolId === "agent" && instance !== null) return instance;
  }
  return useStore.getState().activeSessionId;
}

/**
 * Detach the focused agent session: record it as reattachable and close the pane
 * that viewed it, leaving the session running headless (M5 item 15). The pane is
 * closed only once the record is made, and never when it is the last pane in the
 * window — an empty dock has no way back — in which case the pane stays and
 * becomes its own Resume tombstone.
 */
function detachFocusedSession(): void {
  const sessionId = detachTargetSessionId();
  if (sessionId === null) {
    useStore.getState().pushToast("warn", "Focus an agent session to detach it.");
    return;
  }
  void useStore
    .getState()
    .detachSession(sessionId)
    .then(() => {
      if (!(sessionId in useStore.getState().detachedSessions)) return; // detach failed
      const dock = dockApiHandle();
      if (dock === null) return;
      const panel = dock.getPanel(paneId("agent", sessionId));
      if (panel !== undefined && dock.panels.length > 1) panel.api.close();
    });
}

/**
 * Whether a new session would be refused, and what the picker says instead.
 *
 * The server caps concurrent sessions (`WORKBENCH_MAX_CONCURRENT_SESSIONS`,
 * enforced in `services/agent_sessions.py`) and counts sessions *working*, not
 * sessions open — so the numbers are read from `/api/agents/limits` rather than
 * counted off the listing, which would grey the row at the wrong moment.
 *
 * Without this the ceiling is discovered *after* the split gesture, a round
 * trip and a generic "New session failed" toast — and the split is abandoned
 * with nothing on screen having mentioned a limit. Before the first listing
 * lands there are no numbers and the row behaves as it always did; the 429
 * toast is still the backstop for a count that moved under an in-flight split.
 */
function newSessionRow(): { detail: string; disabled: boolean } {
  const limits = useStore.getState().sessionLimits;
  if (limits === null || limits.active < limits.max_concurrent) {
    return { detail: activeFolder() || "workspace root", disabled: false };
  }
  return {
    detail:
      `${String(limits.active)} of ${String(limits.max_concurrent)} sessions busy — ` +
      "raise WORKBENCH_MAX_CONCURRENT_SESSIONS",
    disabled: true,
  };
}

/**
 * The Agent tool renders two ways, decided by the pane's id (`../panes.ts`).
 *
 *  - `agent` — the default pane: the session list plus whichever session the
 *    keyboard is on. The panel Workbench has always had;
 *  - `agent#<session_id>` — a pane bound to one conversation. This is the
 *    headline of the pane system: four of them in a 2×2 is four agents working
 *    where you can see all four, and the binding is the session id, so a saved
 *    layout brings back *those* conversations rather than four empty panes.
 *
 * The focused pane is the one you are talking to. A session pane calls
 * `focusSession` when it becomes active, which is what makes `sendChat`,
 * `interrupt` and a `prompt` shortcut mean this conversation — and is why
 * `Chat` itself needed no change to be mounted N times.
 */
export function AgentPanel(props: IDockviewPanelProps) {
  const sessionId = paneInstance(props.api.id);
  return sessionId === null ? (
    <AgentBrowser />
  ) : (
    <SessionPane sessionId={sessionId} api={props.api} />
  );
}

/** One conversation, filling its pane. */
function SessionPane({ sessionId, api }: { sessionId: string; api: DockviewPanelApi }) {
  const live = useStore((s) =>
    s.folders.some((group) =>
      group.sessions.some((session) => session.session_id === sessionId && session.live),
    ),
  );
  const title = useStore(
    (s) =>
      s.folders
        .flatMap((group) => group.sessions)
        .find((session) => session.session_id === sessionId)?.title ?? null,
  );
  // Detached-but-alive: the session is still running here, but the user detached
  // it (M5 item 15), so a pane onto it must *offer* Resume rather than silently
  // reconnect — "a restored pane is vetted before it is believed". Distinct from
  // the truly-gone case below, which is the quiet dead tombstone.
  const detached = useStore((s) => sessionId in s.detachedSessions);
  // Sessions arrive after the layout does, so "not in the list" means "not yet"
  // until the list has something in it. Judging a restored pane before then
  // would put a "stopped" note over a conversation that is about to appear.
  const loaded = useStore((s) => s.folders.length > 0);
  // The one condition under which this pane talks to the server: a session that
  // is running here and has not been detached out from under the pane.
  const attachable = live && !detached;

  // Both effects wait for `attachable`, and that gate is the whole discipline:
  // `attachSession` opens a socket to `/ws/agent/<id>`, and a restored pane is
  // a *claim* about a session, not evidence of one. A layout saved before the
  // server restarted names an id `SessionManager` no longer has (its sessions
  // are in memory only) — the server answers 4404 and the socket would retry
  // for as long as the tab stayed open, once per dead pane, behind a note
  // correctly saying the session is gone. A *detached* session is alive but the
  // user chose to leave it, so the pane waits for Resume before connecting.
  // `openSession`/`openLiveSession`/`openSessionById` have always looked the
  // session up before connecting; this is a pane doing the same. (`ws.ts`
  // refuses to retry a 4404 too — belt and braces, for a session that dies
  // while its pane is open.)
  useEffect(() => {
    if (!attachable) return;
    useStore.getState().attachSession(sessionId);
  }, [attachable, sessionId]);

  useEffect(() => {
    if (!attachable) return;
    if (api.isActive) useStore.getState().focusSession(sessionId);
    const subscription = api.onDidActiveChange((event) => {
      if (event.isActive) useStore.getState().focusSession(sessionId);
    });
    return () => subscription.dispose();
  }, [api, attachable, sessionId]);

  // The tab follows the conversation: a session is "new session" until its
  // first message names it, and a pane still calling it that is a pane you
  // cannot tell from the other three.
  useEffect(() => {
    if (title !== null) api.setTitle(title);
  }, [api, title]);

  // A detached-but-alive session shows only the Resume tombstone — no chat over
  // it, because the socket is deliberately not open. Resume clears the mark,
  // which lets the attach effect above connect and hydrate (#68).
  if (loaded && attachable === false && live && detached) {
    return (
      <div className="wb-pane-single">
        <div className="wb-pane-note">
          <span className="wb-pane-note-msg u-truncate">
            This session is detached — still running, waiting for you.
          </span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => useStore.getState().reattachSession(sessionId)}
          >
            Resume
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="wb-pane-single">
      {loaded && !live && (
        <div className="wb-pane-note">
          <span className="wb-pane-note-msg u-truncate">
            This session is not running any more.
          </span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => {
              useStore.getState().openSessionById(sessionId);
              focusPanel("agent");
            }}
          >
            Open its transcript
          </button>
        </div>
      )}
      <Chat sessionId={sessionId} />
    </div>
  );
}

/** Reattach a detached session into a pane: open (or focus) its pane, then clear
 * the detached mark so the pane attaches and hydrates its transcript (#68). */
function reattachIntoPane(sessionId: string): void {
  revealPane("agent", sessionId);
  useStore.getState().reattachSession(sessionId);
}

/** One detached session, offered for reattach. Kept out of the folder list below
 * so a session is in exactly one place — its own section when detached. */
function DetachedRow({ session }: { session: SessionInfo }) {
  return (
    <div className="wb-session-row is-detached">
      <span className="wb-dot wb-dot-disk" aria-hidden="true" />
      <span className="wb-session-title u-truncate" title={session.title}>
        {session.title}
      </span>
      <button
        type="button"
        className="wb-btn wb-btn-sm wb-btn-outline"
        onClick={() => reattachIntoPane(session.session_id)}
      >
        Reattach
      </button>
    </div>
  );
}

/** The detached-but-alive sessions, newest first, with their own header — the
 * "Detached" affordance a user returns to. Empty renders nothing. */
function DetachedSessions() {
  // Select the raw slices and derive in the render body — a selector that
  // returned the `.filter().sort()` array would hand `useSyncExternalStore` a
  // fresh reference every call and loop (the pattern `SessionChips` uses).
  const folders = useStore((s) => s.folders);
  const detached = useStore((s) => s.detachedSessions);
  const sessions = folders
    .flatMap((group) => group.sessions)
    .filter((session) => session.session_id in detached)
    .sort((a, b) => b.updated_at - a.updated_at);
  if (sessions.length === 0) return null;
  return (
    <div>
      <div className="wb-sessions-folder u-label">Detached</div>
      {sessions.map((session) => (
        <DetachedRow key={session.session_id} session={session} />
      ))}
    </div>
  );
}

function AgentBrowser() {
  const folders = useStore((s) => s.folders);
  const detached = useStore((s) => s.detachedSessions);
  const activeSessionId = useStore((s) => s.activeSessionId);
  const hasTranscript = useStore((s) => s.transcriptView !== null);
  // Same ceiling, same answer as the pane picker's row — a button that is live
  // here and greyed there would be two accounts of one limit.
  const full = useStore(
    (s) => s.sessionLimits !== null && s.sessionLimits.active >= s.sessionLimits.max_concurrent,
  );

  const newSession = (): void => {
    void useStore.getState().createSessionIn(activeFolder());
  };

  return (
    <div className="wb-agent">
      <div className="wb-sessions">
        <div className="wb-sessions-header">
          <span className="u-label">Sessions</span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            disabled={full}
            title={full ? newSessionRow().detail : undefined}
            onClick={newSession}
          >
            New session
          </button>
        </div>
        <div className="wb-sessions-list">
          {folders.length === 0 && <div className="wb-sessions-none">No sessions yet</div>}
          <DetachedSessions />
          {folders.map((group) => {
            // A detached session lives in its own section above, not here — one
            // session, one place in the list.
            const rows = group.sessions.filter((s) => !(s.session_id in detached));
            if (rows.length === 0) return null;
            return (
              <div key={group.folder}>
                <div className="wb-sessions-folder u-label u-truncate" title={group.folder}>
                  {group.folder === "" ? "workspace root" : group.folder}
                </div>
                {rows.map((session) => (
                  <SessionRow key={session.session_id} session={session} />
                ))}
              </div>
            );
          })}
        </div>
      </div>
      <div className="wb-chat-area">
        {activeSessionId !== null ? (
          <Chat sessionId={activeSessionId} />
        ) : hasTranscript ? (
          <TranscriptView />
        ) : (
          <div className="wb-empty">
            <div className="wb-empty-title">No session selected</div>
            <div className="wb-empty-hint">
              Pick a session above, or press <span className="wb-keycap">Ctrl</span>{" "}
              <span className="wb-keycap">K</span> and run “New agent session here”.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ---- status bar -------------------------------------------------------------

/** Chips beyond this collapse into a count, so a fleet never eats the bar. */
const MAX_CHIPS = 4;

function SessionChip({ session }: { session: SessionInfo }) {
  const state = useStore((s) => s.sessionStates[session.session_id] ?? session.state);
  const flags = useStore((s) => s.sessionFlags[session.session_id]);
  const active = useStore((s) => s.activeSessionId === session.session_id);
  const v = statusVisual(state, flags);
  return (
    <button
      type="button"
      className={"wb-status-chip" + (active ? " is-active" : "")}
      title={`${session.title} — ${v.label}`}
      onClick={() => useStore.getState().openSession(session)}
    >
      <span
        className={"wb-dot" + (v.pulse ? " u-agent-pulse" : "")}
        style={{ background: v.color }}
        role="img"
        aria-label={v.label}
      />
      <span className="wb-status-chip-title u-truncate">{session.title}</span>
    </button>
  );
}

/** Centre of the status bar: one chip per live session, overflow as a count. */
function SessionChips() {
  const folders = useStore((s) => s.folders);
  const live = folders.flatMap((group) => group.sessions).filter((session) => session.live);
  const shown = live.slice(0, MAX_CHIPS);
  const overflow = live.length - shown.length;
  return (
    <>
      {shown.map((session) => (
        <SessionChip key={session.session_id} session={session} />
      ))}
      {overflow > 0 && (
        <span className="wb-status-more u-tabular" title={`${overflow} more live sessions`}>
          +{overflow}
        </span>
      )}
    </>
  );
}

function countStates(states: Record<string, SessionState>, want: SessionState): number {
  return Object.values(states).filter((state) => state === want).length;
}

/** Right end of the status bar: the fleet at a glance, and what it cost. */
function SessionCounts() {
  const states = useStore((s) => s.sessionStates);
  const lastCostUsd = useStore((s) => s.lastCostUsd);
  const attention = countStates(states, "needs_attention");
  const working = countStates(states, "working");
  return (
    <>
      {attention > 0 && (
        <span className="wb-status-count" title={`${attention} sessions need attention`}>
          <span
            className="wb-dot"
            style={{ background: "var(--agent-attention)" }}
            role="img"
            aria-label="Needs attention"
          />
          <span className="u-tabular">{attention}</span>
        </span>
      )}
      {working > 0 && (
        <span className="wb-status-count" title={`${working} sessions working`}>
          <span
            className="wb-dot u-agent-pulse"
            style={{ background: "var(--agent-working)" }}
            role="img"
            aria-label="Working"
          />
          <span className="u-tabular">{working}</span>
        </span>
      )}
      {lastCostUsd !== null && (
        <span className="wb-status-cost u-tabular" title="Cost of the last finished turn">
          ${lastCostUsd.toFixed(4)}
        </span>
      )}
    </>
  );
}

// ---- registration -----------------------------------------------------------

/**
 * The Agent tab's badge: one dot when any session is waiting on the user, so a
 * question does not sit unseen behind another panel (DESIGN.md §6.4, dot-only —
 * the count belongs in the status bar). Contributed by the descriptor rather
 * than drawn by the tab component, which names no panel.
 */
function AttentionBadge() {
  const attention = useStore((s) =>
    Object.values(s.sessionStates).some((state) => state === "needs_attention"),
  );
  if (!attention) return null;
  return (
    <span
      className="wb-tab-attention-dot"
      role="img"
      aria-label="A session needs attention"
      title="A session needs attention"
    />
  );
}

/** Alt+1..9 — the n-th most recent session, live or resumable. */
const sessionJumps = Array.from({ length: 9 }, (_, i) => ({
  id: `session.jump.${String(i + 1)}`,
  title: `Jump to session ${String(i + 1)}`,
  when: () => recentSessions().length > i,
  detail: () => recentSessions()[i]?.title ?? "",
  run: () => {
    const session = recentSessions()[i];
    if (session !== undefined) useStore.getState().openSession(session);
  },
}));

const jumpChords = Object.fromEntries(
  sessionJumps.map((command, i) => [command.id, [`Alt+${String(i + 1)}`]]),
);

export const agentTool: WorkbenchTool = {
  id: "agent",
  title: "Agent",
  panel: {
    component: AgentPanel,
    defaultLocation: { area: "right", size: 380 },
    badge: AttentionBadge,
    // Plural, and this is the one that matters: N sessions on screen at once.
    singleton: false,
    instances: {
      options: () => [
        {
          id: "agent.new",
          title: "New agent session",
          category: "Agent",
          ...newSessionRow(),
          // Creates the session first and answers with its id, so the pane is
          // born bound to it — an unbound pane would have nothing a saved
          // layout could bring back.
          key: () => useStore.getState().createSessionIn(activeFolder()),
        },
        // Live sessions only. A finished one is a transcript, and a transcript
        // is something you read in the Agent panel, not a pane you work in.
        ...liveSessions().map((session) => ({
          id: `agent.${session.session_id}`,
          title: session.title,
          detail: session.folder === "" ? "workspace root" : session.folder,
          category: "Agent sessions",
          key: () => session.session_id,
        })),
      ],
      titleFor: (key) =>
        useStore
          .getState()
          .folders.flatMap((group) => group.sessions)
          .find((session) => session.session_id === key)?.title ?? "Session",
    },
  },
  // A `prompt` shortcut is inserted into the chat box, so this panel comes
  // forward first — declared here so `commands.ts` routes by capability.
  shortcutKinds: ["prompt"],
  commands: [
    {
      id: "session.new",
      title: "New agent session here",
      detail: () => activeFolder() || "workspace root",
      run: () => void useStore.getState().createSessionIn(activeFolder()),
    },
    {
      // Detach the focused session: it keeps running headless and its pane
      // closes as a view (M5 item 15). Chordless — a deliberate, occasional act,
      // not a reflex worth spending an `Alt` a `shortcuts.md` file could bind.
      id: "session.detach",
      title: "Detach this session",
      detail: () => "keeps it running; reattach from Detached",
      when: () => detachTargetSessionId() !== null,
      run: detachFocusedSession,
    },
    ...sessionJumps,
    // The plan card lives inside this panel's chat, so its commands are this
    // capability's — not a second tool for a thing with no panel of its own.
    ...planCommands,
  ],
  shortcuts: { ...jumpChords, ...planShortcuts },
  statusContributions: [
    { region: "center", component: SessionChips },
    { region: "right", component: SessionCounts },
  ],
  // The context-bridge MCP tools this capability puts in every session's
  // context are declared once, server-side, in services/agent_tools.py — the
  // registry the SDK reads and the tests budget. Nothing in the UI reads them.
};
