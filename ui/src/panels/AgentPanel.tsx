import type { IDockviewPanelProps } from "dockview";

import { relativeTime } from "../relativeTime";
import { useStore } from "../store";
import type { SessionInfo } from "../types";
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

export function AgentPanel(_props: IDockviewPanelProps) {
  const folders = useStore((s) => s.folders);
  const activeSessionId = useStore((s) => s.activeSessionId);
  const hasTranscript = useStore((s) => s.transcriptView !== null);

  const newSession = (): void => {
    const s = useStore.getState();
    const folder =
      s.activePath !== null && s.activePath.includes("/")
        ? s.activePath.slice(0, s.activePath.lastIndexOf("/"))
        : "";
    void s.createSessionIn(folder);
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
