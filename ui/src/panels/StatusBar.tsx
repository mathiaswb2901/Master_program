/**
 * The 24px bar under the dock: where you are (left), what every session is
 * doing (centre), and what it is costing (right). Glanceable without opening a
 * panel — the whole point of the flow layer.
 */

import { useStore } from "../store";
import type { SessionInfo, SessionState } from "../types";
import { statusVisual } from "./Chat";

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

function countStates(states: Record<string, SessionState>, want: SessionState): number {
  return Object.values(states).filter((state) => state === want).length;
}

export function StatusBar() {
  const workspace = useStore((s) => s.tree?.name ?? "workspace");
  const activePath = useStore((s) => s.activePath);
  const dirty = useStore(
    (s) => s.openFiles.find((f) => f.path === s.activePath)?.dirty ?? false,
  );
  const folders = useStore((s) => s.folders);
  const states = useStore((s) => s.sessionStates);
  const lastCostUsd = useStore((s) => s.lastCostUsd);

  const live = folders.flatMap((group) => group.sessions).filter((session) => session.live);
  const shown = live.slice(0, MAX_CHIPS);
  const overflow = live.length - shown.length;
  const attention = countStates(states, "needs_attention");
  const working = countStates(states, "working");

  return (
    <div className="wb-statusbar">
      <div className="wb-status-left">
        <span className="wb-status-workspace u-truncate">{workspace}</span>
        {activePath !== null && (
          <>
            <span className="wb-status-sep" aria-hidden="true">
              /
            </span>
            <span className="wb-status-file u-truncate" title={activePath}>
              {activePath}
            </span>
            {dirty && (
              <span
                className="wb-status-dirty"
                role="img"
                aria-label="Unsaved changes"
                title="Unsaved changes"
              />
            )}
          </>
        )}
      </div>

      <div className="wb-status-center">
        {shown.map((session) => (
          <SessionChip key={session.session_id} session={session} />
        ))}
        {overflow > 0 && (
          <span className="wb-status-more u-tabular" title={`${overflow} more live sessions`}>
            +{overflow}
          </span>
        )}
      </div>

      <div className="wb-status-right">
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
      </div>
    </div>
  );
}
