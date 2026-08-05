import type { IDockviewPanelProps } from "dockview";

import type { WorkbenchTool } from "../registry";
import { relativeTime } from "../relativeTime";
import { useStore } from "../store";
import type { SessionInfo, SessionState } from "../types";
import { Chat, statusVisual, TranscriptView } from "./Chat";

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

export function AgentPanel(_props: IDockviewPanelProps) {
  const folders = useStore((s) => s.folders);
  const activeSessionId = useStore((s) => s.activeSessionId);
  const hasTranscript = useStore((s) => s.transcriptView !== null);

  const newSession = (): void => {
    void useStore.getState().createSessionIn(activeFolder());
  };

  return (
    <div className="wb-agent">
      <div className="wb-sessions">
        <div className="wb-sessions-header">
          <span className="u-label">Sessions</span>
          <button type="button" className="wb-btn wb-btn-sm wb-btn-outline" onClick={newSession}>
            New session
          </button>
        </div>
        <div className="wb-sessions-list">
          {folders.length === 0 && <div className="wb-sessions-none">No sessions yet</div>}
          {folders.map((group) => (
            <div key={group.folder}>
              <div className="wb-sessions-folder u-label u-truncate" title={group.folder}>
                {group.folder === "" ? "workspace root" : group.folder}
              </div>
              {group.sessions.map((session) => (
                <SessionRow key={session.session_id} session={session} />
              ))}
            </div>
          ))}
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
    ...sessionJumps,
  ],
  shortcuts: jumpChords,
  statusContributions: [
    { region: "center", component: SessionChips },
    { region: "right", component: SessionCounts },
  ],
  // The context-bridge MCP tools this capability puts in every session's
  // context are declared once, server-side, in services/agent_tools.py — the
  // registry the SDK reads and the tests budget. Nothing in the UI reads them.
};
