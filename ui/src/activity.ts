/**
 * Live agent activity on the UI side: the reading, and a store next to it.
 *
 * Everything the fleet view has to *say* is computed here as pure functions over
 * the rows the server sends, so the rules are unit-tested rather than trusted
 * (`activity.test.ts`):
 *
 * - **"Now" and "just did" are derived, not sent.** The server keeps one
 *   bounded window per session, ordered by when each call started; `current`
 *   is the newest entry still running and `last` the most recently *settled*
 *   one (by `settled_at`, not by position — two calls in flight can settle out
 *   of order). Deriving them here keeps one authority for what a window holds.
 * - **A session with nothing in its window is a reading, not a gap.** An idle
 *   fleet is the common case, so "open, nothing running" has a rendering.
 * - **Dropped entries are shown.** The window is capped by construction; a
 *   count of what fell out is the difference between a bounded view and a
 *   lying one.
 *
 * The store is this capability's own zustand instance rather than a slice of
 * `store.ts`: nothing outside this module and its panel reads it, which is
 * exactly the condition CLAUDE.md puts on a second store (`usage.ts` is the
 * shipped precedent). It subscribes to the shared `/ws/events` socket for its
 * own frames.
 */

import { create } from "zustand";

import * as api from "./api";
import type { ActivityEntry, ActivitySnapshot, SessionActivity, WorkspaceEvent } from "./types";
import { ReconnectingSocket } from "./ws";

// ---- reading one session's window --------------------------------------------

/** What this session is doing *now*: the newest call still running, or null. */
export function currentEntry(session: SessionActivity): ActivityEntry | null {
  return session.entries.find((entry) => entry.settled_at === null) ?? null;
}

/**
 * What it just did: the most recently settled call.
 *
 * By `settled_at` rather than by list position — entries are ordered by when
 * they *started*, and an agent that fires two calls at once routinely settles
 * them in the other order.
 */
export function lastSettledEntry(session: SessionActivity): ActivityEntry | null {
  let best: ActivityEntry | null = null;
  for (const entry of session.entries) {
    if (entry.settled_at === null) continue;
    if (best === null || entry.settled_at > (best.settled_at ?? 0)) best = entry;
  }
  return best;
}

/** Sessions with a call in flight — what "active right now" means everywhere in
 * this capability, including the status-bar reading. */
export function activeSessions(sessions: readonly SessionActivity[]): SessionActivity[] {
  return sessions.filter((session) => currentEntry(session) !== null);
}

/** Calls in flight across the whole fleet. One session can hold several. */
export function runningCalls(sessions: readonly SessionActivity[]): number {
  return sessions.reduce(
    (total, session) => total + session.entries.filter((e) => e.settled_at === null).length,
    0,
  );
}

/** The folder line under a title: the workspace root has no name of its own. */
export function folderLabel(folder: string): string {
  return folder === "" ? "workspace root" : folder;
}

/** How a settled call reads: colourless words, because colour is never the only
 * signal (DESIGN.md §7). */
export function outcomeLabel(entry: ActivityEntry): string {
  if (entry.settled_at === null) return "Running";
  return entry.ok === false ? "Failed" : "Done";
}

/** Fleet order: most recently active first, so the row that just changed is the
 * row at the top. Ties break on session id, which keeps the order stable rather
 * than letting equal timestamps reshuffle on every frame. */
export function orderSessions(sessions: readonly SessionActivity[]): SessionActivity[] {
  return [...sessions].sort(
    (a, b) => b.active_at - a.active_at || a.session_id.localeCompare(b.session_id),
  );
}

/**
 * Fold one `/ws/events` frame into the rows this window holds.
 *
 * Pure, and the only place the live path and the load path differ: the frame
 * carries whole rows for the sessions that moved plus the ids of the ones that
 * left, so merging is a replace-by-id and a delete — never a patch that has to
 * agree with the server about what a window contained.
 */
export function mergeSessions(
  current: readonly SessionActivity[],
  changed: readonly SessionActivity[],
  removed: readonly string[],
): SessionActivity[] {
  const gone = new Set(removed);
  const byId = new Map<string, SessionActivity>();
  for (const session of current) {
    if (!gone.has(session.session_id)) byId.set(session.session_id, session);
  }
  for (const session of changed) {
    if (!gone.has(session.session_id)) byId.set(session.session_id, session);
  }
  return orderSessions([...byId.values()]);
}

// ---- the store ---------------------------------------------------------------

interface ActivityStore {
  /** Null until the first fetch answers — distinct from an empty fleet, which
   * is a server saying "no sessions are running". */
  snapshot: ActivitySnapshot | null;
  init: () => void;
  refresh: () => Promise<void>;
}

let started = false;

/** Reset the module-level "already subscribed" latch. Tests only: the socket is
 * opened once for the life of the page in the app. */
export function resetActivityStoreForTests(): void {
  started = false;
  useActivityStore.setState({ snapshot: null });
}

export const useActivityStore = create<ActivityStore>((set, get) => ({
  snapshot: null,

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
    // Activity rides the shared bus; this socket reads only its own frames —
    // the same thing `usage.ts` and `officeHost.ts` do, and the reason the app
    // store holds no state this capability owns.
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "session_activity") return;
        set((state) => {
          const snapshot = state.snapshot;
          if (snapshot === null) return state; // the fetch below will bring it
          return {
            snapshot: {
              ...snapshot,
              sessions: mergeSessions(snapshot.sessions, event.sessions, event.removed),
            },
          };
        });
      },
      // Every reconnect re-reads: frames that arrived while the socket was down
      // are gone, and this is live state with no history to replay.
      onOpen: () => {
        void get().refresh();
      },
    });
  },

  refresh: async () => {
    try {
      const snapshot = await api.getActivity();
      set({ snapshot: { ...snapshot, sessions: orderSessions(snapshot.sessions) } });
    } catch {
      // A server that cannot answer leaves the last picture standing with its
      // stamps ageing, which is the honest reading of the situation.
    }
  },
}));
