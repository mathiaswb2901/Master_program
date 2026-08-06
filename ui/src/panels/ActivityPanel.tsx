/**
 * Live agent activity — "see everywhere Claude is editing".
 *
 * A whole capability in one module (plus `activity.ts` and its stylesheet): the
 * panel, the status-bar reading, the command that opens it, and the descriptor
 * that registers all three. It edits no shared file except the one line in
 * `tools.ts`.
 *
 * **What this is, and what it is not.** The Provenance surfaces answer *who
 * wrote the file I am looking at*, after the fact and conservatively. This
 * answers *what is the fleet doing this second* — every live session at once,
 * whether or not this window has that conversation open, because the frames it
 * is built from (`tool_use`/`tool_settled`) only ever reached the socket
 * belonging to one chat. Four agents working is four rows changing at four
 * different rhythms, and that is the whole product claim.
 *
 * Three design decisions worth stating, because each is a thing a later edit
 * could quietly undo:
 *
 * 1. **The row is O(1), not O(tool calls).** Each session shows what it is
 *    doing *now* and what it *just did*, replaced in place. The rest of its
 *    bounded window is behind a disclosure, and what the window dropped is
 *    counted on the row — a view that silently forgets is not a view you can
 *    read a number off.
 * 2. **Idle is a designed state.** An idle fleet is the common case. "No agent
 *    sessions running" and "open, nothing running" are sentences, not an empty
 *    panel that reads as broken.
 * 3. **No new colours.** The dot is `statusVisual` — the same §2.6 agent-status
 *    vocabulary as the session rows and the status chips — and a failed call
 *    says "Failed" as well as wearing `--agent-error` (§7).
 */

import type { IDockviewPanelProps } from "dockview";
import { memo, useEffect, useState } from "react";

import {
  activeSessions,
  currentEntry,
  folderLabel,
  lastSettledEntry,
  outcomeLabel,
  runningCalls,
  useActivityStore,
} from "../activity";
import { openPanel } from "../dock";
import type { WorkbenchTool } from "../registry";
import { relativeTime } from "../relativeTime";
import { useStore } from "../store";
import type { ActivityEntry, ActivitySnapshot, SessionActivity, SessionState } from "../types";
import { statusVisual } from "./Chat";

import "../styles/activity.css";

const TOOL_ID = "activity";

/** How often the relative stamps are recomputed. `relativeTime` is coarse
 * (under a minute reads "now"), so a minute is its full resolution — and a
 * faster tick would be motion for its own sake (DESIGN.md §5). Rows themselves
 * update the moment a frame lands, which is what makes this feel live. */
const TICK_MS = 60_000;

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/**
 * The glyph, at the size its surface calls for.
 *
 * One shape, three surfaces, and they do not agree on size: the tab and the
 * status bar want a 14px mark at the weight of the text beside them, while
 * DESIGN.md §6.10 fixes an empty state's icon at **32px, 1.5px stroke**. Sizing
 * is therefore a prop rather than baked into the path — a shared glyph that
 * hard-codes one caller's dimensions makes every other caller wrong, and the
 * stroke has to scale with it or a 32px mark drawn at 1.2 reads as a hairline.
 */
function PulseIcon({ size = 14, strokeWidth = 1.2 }: { size?: number; strokeWidth?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1 8h3l2.2-4.5L9 12.5 10.8 8H15" />
    </svg>
  );
}

/** DESIGN.md §6.10, as numbers this module can be checked against. */
export const EMPTY_STATE_ICON = { size: 32, strokeWidth: 1.5 } as const;

/** The store, started by whichever surface mounts first (the status item is
 * always mounted, so in practice that is boot). */
function useActivity(): { snapshot: ActivitySnapshot | null; now: number } {
  const snapshot = useActivityStore((s) => s.snapshot);
  useEffect(() => {
    useActivityStore.getState().init();
  }, []);
  return { snapshot, now: useNow() };
}

// ---- one line of activity -----------------------------------------------------

/**
 * A tool call, as one line: what it is, and — where it named a workspace file —
 * a button that opens it.
 *
 * The target is a real `<button>` rather than a styled span, because opening the
 * file an agent is editing *right now* is the one action this panel offers and
 * it has to be reachable from the keyboard (§7). A call that named no file (a
 * Bash, a Grep, a WebFetch) renders as text — there is nothing to open, and a
 * dead-looking control would be worse than none.
 */
function ActivityLine({ entry }: { entry: ActivityEntry }) {
  const target = entry.target;
  if (target === null) {
    return <span className="wb-activity-line u-truncate">{entry.summary}</span>;
  }
  return (
    <button
      type="button"
      className="wb-activity-line wb-activity-target u-truncate"
      title={`Open ${target}`}
      onClick={() => void useStore.getState().openFile(target)}
    >
      {entry.summary}
    </button>
  );
}

/** One session's card: the dot, what it is doing, what it just did, and — folded
 * away — the rest of its bounded window. */
function SessionCardBody({ session, now }: { session: SessionActivity; now: number }) {
  const current = currentEntry(session);
  const last = lastSettledEntry(session);
  // The server's activity feed says nothing about *state* — that is the app
  // store's, kept fresh by `session_status` on the same socket, and it is what
  // makes "this one is blocked on a permission card" visible from here. A
  // session this window has not listed yet still gets an honest dot: a call in
  // flight is a session working.
  const known = useStore((s) => s.sessionStates[session.session_id]);
  const flags = useStore((s) => s.sessionFlags[session.session_id]);
  const state: SessionState = known ?? (current === null ? "idle" : "working");
  const visual = statusVisual(state, flags);
  const earlier = session.entries.filter(
    (entry) => entry.entry_id !== current?.entry_id && entry.entry_id !== last?.entry_id,
  );
  return (
    <li className="wb-activity-session" data-session={session.session_id}>
      <header className="wb-activity-session-head">
        <span
          className={"wb-dot" + (visual.pulse ? " u-agent-pulse" : "")}
          style={{ background: visual.color }}
          role="img"
          aria-label={visual.label}
        />
        <button
          type="button"
          className="wb-activity-session-title u-truncate"
          title={`${session.title} — open this conversation`}
          onClick={() => useStore.getState().openSessionById(session.session_id)}
        >
          {session.title}
        </button>
        <span className="wb-activity-session-age u-tabular" title="Last activity">
          {relativeTime(session.active_at, now / 1000)}
        </span>
      </header>
      <p className="wb-activity-folder u-truncate">{folderLabel(session.folder)}</p>

      <div className="wb-activity-now">
        <span className="u-label wb-activity-phase">Now</span>
        {current === null ? (
          <span className="wb-activity-quiet">Nothing running</span>
        ) : (
          <ActivityLine entry={current} />
        )}
      </div>

      {last !== null && (
        <div className="wb-activity-then">
          <span className="u-label wb-activity-phase">Just did</span>
          <ActivityLine entry={last} />
          <span
            className={"wb-activity-outcome" + (last.ok === false ? " is-failed" : "")}
            style={last.ok === false ? { color: "var(--agent-error)" } : undefined}
          >
            {outcomeLabel(last)}
          </span>
        </div>
      )}

      {earlier.length > 0 && (
        <details className="wb-activity-earlier">
          <summary>
            Earlier in this session ({earlier.length})
            {session.dropped > 0 ? `, ${session.dropped} dropped` : ""}
          </summary>
          <ul className="wb-activity-earlier-list">
            {earlier.map((entry) => (
              <li key={entry.entry_id} className="wb-activity-earlier-row">
                <ActivityLine entry={entry} />
                <span className="wb-activity-outcome">{outcomeLabel(entry)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {earlier.length === 0 && session.dropped > 0 && (
        <p className="wb-activity-dropped">{session.dropped} earlier calls dropped</p>
      )}
    </li>
  );
}

/**
 * The row is O(1) **in renders too**, not only in what it holds.
 *
 * Every `session_activity` frame replaces the whole snapshot object, so without
 * this boundary one session's tool call re-renders every other session's card —
 * up to four times a second, for rows whose data did not move. That is precisely
 * the re-render storm the server's 250 ms coalescing exists to prevent, arriving
 * one layer later, and it gets worse with exactly the thing this feature is for:
 * four agents working at once.
 *
 * `session` and `now` are the whole prop surface and both are reference-stable
 * across a frame — `mergeSessions` replaces only the rows the frame carried and
 * keeps the identity of the rest (`activity.test.ts` pins that, because it is
 * what makes this memo do anything), and `now` moves once a minute. The per-row
 * store subscriptions inside the body are unaffected: those still re-render this
 * card when *this* session's state or flags change, which is the point of them.
 */
export const SessionCard = memo(SessionCardBody);

// ---- the panel ----------------------------------------------------------------

export function ActivityPanel(_props: IDockviewPanelProps) {
  const { snapshot, now } = useActivity();
  return <ActivityBody snapshot={snapshot} now={now} />;
}

/** The panel's whole rendering, as a function of the snapshot. Exported for the
 * tests that hold the fleet states a browser journey cannot stage in time. */
export function ActivityBody({
  snapshot,
  now,
}: {
  snapshot: ActivitySnapshot | null;
  now: number;
}) {
  if (snapshot === null) {
    return (
      <div className="wb-activity">
        <p className="wb-activity-note">Reading activity…</p>
      </div>
    );
  }
  const sessions = snapshot.sessions;
  const working = activeSessions(sessions).length;
  return (
    <div className="wb-activity">
      <header className="wb-activity-head">
        <span className="u-label">Live agent activity</span>
        <span className="wb-activity-summary u-tabular">
          {sessions.length === 0
            ? "No sessions"
            : `${String(working)} of ${String(sessions.length)} working`}
        </span>
      </header>

      {sessions.length === 0 ? (
        <div className="wb-activity-empty" data-testid="activity-empty">
          <span className="wb-activity-empty-icon">
            <PulseIcon {...EMPTY_STATE_ICON} />
          </span>
          <p className="wb-activity-empty-title">No agent sessions running</p>
          <p className="wb-activity-note">
            Start one from the Agent panel and every tool call it makes appears here the moment it
            starts — including sessions you have never opened a chat for. This is a live view, so it
            is empty whenever the fleet is.
          </p>
        </div>
      ) : (
        <>
          <ul className="wb-activity-fleet">
            {sessions.map((session) => (
              <SessionCard key={session.session_id} session={session} now={now} />
            ))}
          </ul>
          <p className="wb-activity-note wb-activity-source">
            The last {snapshot.max_entries_per_session} tool calls per session, up to{" "}
            {snapshot.max_sessions} sessions — a live view, not a history, and nothing here is
            written to disk.
            {snapshot.dropped_sessions > 0
              ? ` ${String(snapshot.dropped_sessions)} older sessions have dropped out of it.`
              : ""}
          </p>
        </>
      )}
    </div>
  );
}

// ---- status bar ---------------------------------------------------------------

/**
 * The glance: how much of the fleet is touching something right now.
 *
 * Deliberately *not* the working count the Agent tool already contributes — that
 * one counts sessions in the `working` state, which includes an agent thinking
 * or streaming text. This counts sessions with a **tool call in flight**, names
 * what they are on in its tooltip, and opens the panel. Renders nothing when
 * nothing is running (§6.7: counts hide at zero), which is most of the time.
 */
function ActivityStatus() {
  const { snapshot } = useActivity();
  return <ActivityReading snapshot={snapshot} />;
}

export function ActivityReading({ snapshot }: { snapshot: ActivitySnapshot | null }) {
  if (snapshot === null) return null;
  const active = activeSessions(snapshot.sessions);
  if (active.length === 0) return null;
  const calls = runningCalls(snapshot.sessions);
  const title = [
    `${String(active.length)} ${active.length === 1 ? "session is" : "sessions are"} running a tool` +
      (calls === active.length ? "" : ` (${String(calls)} calls)`),
    ...active.map((session) => `${session.title}: ${currentEntry(session)?.summary ?? ""}`),
    "Open live agent activity",
  ].join(" — ");
  return (
    <button
      type="button"
      className="wb-activity-status"
      title={title}
      onClick={() => openPanel(TOOL_ID)}
    >
      <span className="wb-activity-status-icon">
        <PulseIcon />
      </span>
      <span className="u-tabular">{active.length}</span>
    </button>
  );
}

// ---- registration -------------------------------------------------------------

export const activityTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Activity",
  icon: PulseIcon,
  panel: {
    component: ActivityPanel,
    defaultLocation: { area: "right", size: 380 },
    // Not in the startup layout: this is something you open when you want to
    // know what the fleet is doing, and the status reading is the always-on
    // half. Singleton — every pane of it would render the same fleet, and a
    // pane needs something to be *pointed at* to be worth having twice.
    openByDefault: false,
  },
  commands: [
    {
      id: "activity.open",
      title: "Show live agent activity",
      detail: () => "what every session is touching right now",
      run: () => openPanel(TOOL_ID),
    },
  ],
  // No chord: a registered chord beats a user's `shortcuts.md` one and `Alt` is
  // all that file may bind. This is a glance, not a hot path (see Scratchpad).
  statusContributions: [{ region: "right", component: ActivityStatus }],
};
