/**
 * Objectives on the UI side (plan §3): a session's goal, and its status **derived
 * from the validation evidence** — never a stored or asserted "done".
 *
 * The design mirrors `mission.ts`: this module derives no validation state of its
 * own. What a goal's status *is* comes from the same `ValidationResult`s the
 * Review panel holds (`validation.ts`), joined here by the objective's subject —
 * so the objective badge and the Review badge are two readings of one result,
 * never two computations of it. Approval arrives on `/ws/events` as a whole
 * result, and because the status is derived live over that store, an objective
 * flips to `met` the instant the approval lands, with no objective-specific
 * socket.
 *
 * The store below holds only the **goal text** (statement + acceptance) per named
 * session, plus the session list the picker needs — a second zustand instance in
 * this module, read by nothing outside this capability and its panel, exactly the
 * condition CLAUDE.md puts on a store that is not a slice of `store.ts`.
 *
 * **No singleton.** There is no `store.activeObjective`: a goal is looked up by
 * its session id, two sessions carry two goals, and a pane is a view onto one.
 */

import { create } from "zustand";

import * as api from "./api";
import type { NamedSession, Objective, ObjectiveStatus, ValidationResult } from "./types";
import { orderResults, type StatusVisual } from "./validation";

// ---- the status join (pure; unit-tested, never trusted) ---------------------

/** The latest `ValidationResult` whose subject is this objective, or null.
 * `subject.kind === "objective"` disambiguates it from a `session_output` result
 * that shares the session id as its ref. "Latest" is `orderResults`' own ordering
 * (created_at, then id) — the same the server uses — so the two agree. */
export function latestForObjective(
  results: readonly ValidationResult[],
  sessionId: string,
): ValidationResult | null {
  const mine = results.filter(
    (r) => r.subject.kind === "objective" && r.subject.ref === sessionId,
  );
  if (mine.length === 0) return null;
  const ordered = orderResults(mine);
  return ordered[ordered.length - 1] ?? null;
}

/**
 * Status from the goal and its latest evidence — the same table the server
 * derives (`services/sessions.py`), kept in lockstep on purpose:
 *
 * - `open` when there is no objective or no evidence, **or** the evidence passes
 *   but no human has signed it off (approval is the gate);
 * - `met` when the latest result carries a human approval;
 * - `at-risk` at an unapproved `medium`, `failing` at unapproved `high`/`blocked`.
 */
export function objectiveStatus(
  objective: Objective | null,
  latest: ValidationResult | null,
): ObjectiveStatus {
  if (objective === null || latest === null) return "open";
  if (latest.approval !== null) return "met";
  if (latest.risk === "high" || latest.risk === "blocked") return "failing";
  if (latest.risk === "medium") return "at-risk";
  return "open";
}

// ---- the status visual (reuses the semantic ramp — no new colour) -----------

/** An objective status onto the existing semantic tokens — invents nothing, the
 * same discipline the risk badge follows (`validation.ts`):
 * `met → --success`, `at-risk → --warn`, `failing → --error`, and `open` the
 * neutral "not judged yet" grey the blocked/skip states already use. */
const STATUS_VISUAL: Record<ObjectiveStatus, StatusVisual> = {
  open: { token: "--agent-idle", bg: "--agent-idle-bg", label: "Open" },
  met: { token: "--success", bg: "--success-bg", label: "Met" },
  "at-risk": { token: "--warn", bg: "--warn-bg", label: "At risk" },
  failing: { token: "--error", bg: "--error-bg", label: "Failing" },
};

export const objectiveStatusVisual = (status: ObjectiveStatus): StatusVisual =>
  STATUS_VISUAL[status];

// ---- the store ---------------------------------------------------------------

interface ObjectiveStore {
  /** The goal text per named session, as last read from the server. `undefined`
   * = not loaded yet; `null` = loaded, none set. Never a status — that is
   * derived live from the validation store. */
  objectives: Record<string, Objective | null>;
  /** Named sessions for the current workspace, for the index and the picker. */
  sessions: NamedSession[];
  init: () => void;
  /** Re-read the named-session list (which sessions exist to carry a goal). */
  refresh: () => Promise<void>;
  /** Read one session's objective. */
  load: (sessionId: string) => Promise<void>;
  /** Bind or re-state a goal; refreshes the entry from the server's answer. */
  set: (sessionId: string, statement: string, acceptance: string | null) => Promise<void>;
  /** Drop a goal. */
  clear: (sessionId: string) => Promise<void>;
}

let started = false;

/** Reset the module latch and the store. Tests only. */
export function resetObjectiveStoreForTests(): void {
  started = false;
  useObjectiveStore.setState({ objectives: {}, sessions: [] });
}

export const useObjectiveStore = create<ObjectiveStore>((set, get) => ({
  objectives: {},
  sessions: [],

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
  },

  refresh: async () => {
    try {
      const workspace = await api.getWorkspace();
      const { sessions } = await api.listNamedSessions(workspace.root);
      set({
        sessions,
        // Seed the goal map from the manifests we just read — a session that
        // already carries a goal shows it in the index without a second fetch.
        objectives: {
          ...get().objectives,
          ...Object.fromEntries(sessions.map((s) => [s.id, s.objective ?? null])),
        },
      });
    } catch {
      // Never fatal: without the list the index is empty, which reads as "no
      // objectives yet" — the honest degraded state.
    }
  },

  load: async (sessionId) => {
    try {
      const view = await api.getObjective(sessionId);
      set((s) => ({ objectives: { ...s.objectives, [sessionId]: view.objective } }));
    } catch {
      set((s) => ({ objectives: { ...s.objectives, [sessionId]: null } }));
    }
  },

  set: async (sessionId, statement, acceptance) => {
    const view = await api.setObjective(sessionId, { statement, acceptance });
    set((s) => ({ objectives: { ...s.objectives, [sessionId]: view.objective } }));
  },

  clear: async (sessionId) => {
    await api.clearObjective(sessionId);
    set((s) => ({ objectives: { ...s.objectives, [sessionId]: null } }));
  },
}));
