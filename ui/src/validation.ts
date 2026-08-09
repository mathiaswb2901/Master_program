/**
 * Validation on the UI side: the client's own map of results, and the pure
 * rules that turn one `ValidationResult` into a badge, a pill and a reading.
 *
 * Provenance answers "who wrote this file"; validation answers the next
 * question — "and is what they wrote correct" — and the server hands us one
 * `ValidationResult` per run (`models/validation.py`). This module holds those
 * results the way `usage.ts` and `activity.ts` hold theirs: a second zustand
 * instance **in this module**, read by nothing outside this capability and its
 * panel, which is exactly the condition CLAUDE.md puts on a store that is not a
 * slice of `store.ts`.
 *
 * **No singleton.** Results live in a map keyed by `validation_id` — never a
 * `store.activeValidation` — because a session may have several and two Review
 * panes review two different ones side by side. A run and an approval both
 * arrive as a whole `ValidationResult` on `/ws/events`; the client replaces its
 * entry by id, and re-reads `GET /api/validation` on every reconnect (frames
 * that arrived while the socket was down are gone — the usage/activity
 * precedent).
 *
 * Everything below the store is a pure function over a result, so the rules that
 * make the badge honest are unit-tested rather than trusted
 * (`validation.test.ts`):
 *
 * - **The risk badge maps onto the existing semantic ramp — no new colour.**
 *   `pass → --success`, `low → --info`, `medium → --warn`, `high → --error`,
 *   `blocked → --agent-idle` (the "could not judge" grey, distinct from a red
 *   `high` *fail*). DESIGN.md §2.4 spends the accent elsewhere; §2.5/§2.6 already
 *   carry the whole status vocabulary.
 * - **Colour is never the only signal** (DESIGN.md §7): every badge carries a
 *   label, and its dot-only variant an `aria-label`.
 * - **`medium`-or-worse awaits a human.** The one mandatory approval the ROADMAP
 *   requires: a result at `medium`, `high` or `blocked` with no approval yet is
 *   *awaiting review*, and the status-bar reading counts exactly those.
 */

import { create } from "zustand";

import {
  ApiError,
  approveValidation,
  exportValidation,
  getValidations,
  runValidation,
} from "./api";
import type {
  ApproveRequest,
  CheckOutcome,
  EvidenceExport,
  RiskLevel,
  ValidationResult,
  ValidationSpec,
  WorkspaceEvent,
} from "./types";
import { ReconnectingSocket } from "./ws";

// ---- the semantic mapping (no new colour) -----------------------------------

/** How one status renders: a token for its colour, its tint for the pill
 * background, and the word that goes with it (colour is never the only signal). */
export interface StatusVisual {
  /** A DESIGN.md semantic/agent-status token name, e.g. `--warn`. */
  token: string;
  /** The matching `*-bg` tint token, e.g. `--warn-bg`. */
  bg: string;
  /** The human word, for the pill label and the badge's `aria-label`. */
  label: string;
}

/** The risk badge, straight onto the existing ramp — invents nothing. */
const RISK_VISUAL: Record<RiskLevel, StatusVisual> = {
  pass: { token: "--success", bg: "--success-bg", label: "Pass" },
  low: { token: "--info", bg: "--info-bg", label: "Low risk" },
  medium: { token: "--warn", bg: "--warn-bg", label: "Medium risk" },
  high: { token: "--error", bg: "--error-bg", label: "High risk" },
  // The "could not judge" grey — deliberately not a red fail (DESIGN.md §2.6).
  blocked: { token: "--agent-idle", bg: "--agent-idle-bg", label: "Blocked" },
};

/** One evidence line's outcome pill. `skipped` shares the neutral grey with
 * `blocked`: neither is a disagreement, both are "did not / could not judge". */
const OUTCOME_VISUAL: Record<CheckOutcome, StatusVisual> = {
  pass: { token: "--success", bg: "--success-bg", label: "Pass" },
  warn: { token: "--warn", bg: "--warn-bg", label: "Warn" },
  fail: { token: "--error", bg: "--error-bg", label: "Fail" },
  skipped: { token: "--agent-idle", bg: "--agent-idle-bg", label: "Skipped" },
};

export const riskVisual = (risk: RiskLevel): StatusVisual => RISK_VISUAL[risk];
export const outcomeVisual = (outcome: CheckOutcome): StatusVisual => OUTCOME_VISUAL[outcome];

// ---- severity, and who is waiting on a human --------------------------------

/** Least-to-most severe — the server's own ordering (`services/validation.py`).
 * `blocked` sorts highest: a validation that could not judge at all is worse
 * than one that ran and failed a check. */
const RISK_SEVERITY: Record<RiskLevel, number> = {
  pass: 0,
  low: 1,
  medium: 2,
  high: 3,
  blocked: 4,
};

export const riskSeverity = (risk: RiskLevel): number => RISK_SEVERITY[risk];

/** `medium` or worse — the threshold the approval gate and the status reading
 * both use, so the panel's "awaiting" and the bar's count can never disagree. */
export function isMediumOrWorse(risk: RiskLevel): boolean {
  return RISK_SEVERITY[risk] >= RISK_SEVERITY.medium;
}

/**
 * A result the one mandatory human decision is still owed on: `medium`-or-worse
 * and not yet approved. A `pass`/`low` result needs no approval, and one already
 * approved is settled — neither is awaiting.
 */
export function awaitingApproval(result: ValidationResult): boolean {
  return isMediumOrWorse(result.risk) && result.approval === null;
}

/** How many subjects are at `medium`-or-worse awaiting review — the status-bar
 * reading, which hides at zero (§6.7). A quiet bar means nothing needs you. */
export function reviewCount(results: readonly ValidationResult[]): number {
  return results.filter(awaitingApproval).length;
}

// ---- the held map ------------------------------------------------------------

/** Results oldest-first, the order `GET /api/validation` returns and the order
 * the index and picker list them in. `created_at` is an ISO string, so a string
 * compare is the chronological one; the id breaks ties so equal stamps do not
 * reshuffle on every frame. */
export function orderResults(results: readonly ValidationResult[]): ValidationResult[] {
  return [...results].sort(
    (a, b) =>
      a.created_at.localeCompare(b.created_at) ||
      a.validation_id.localeCompare(b.validation_id),
  );
}

/** The newest result held, or null. What a parameterless `validation.export`
 * exports: the thing you just ran is overwhelmingly the thing you meant, and
 * naming *which* one is PR-E's parameterised-command job, not a second surface
 * invented here. Null is a real answer — nothing has been validated — and the
 * command says so rather than doing nothing (AXI shape 2). */
export function newestResult(results: readonly ValidationResult[]): ValidationResult | null {
  const ordered = orderResults(results);
  return ordered[ordered.length - 1] ?? null;
}

/** The latest result for a session's output, or null — the join Mission Control
 * reads (`mission.ts`, the fifth read). Keyed on `subject.kind === "session_output"`
 * so a file result for the same ref never masquerades as the session's. */
export function latestForSession(
  results: readonly ValidationResult[],
  sessionId: string,
): ValidationResult | null {
  const mine = results.filter(
    (r) => r.subject.kind === "session_output" && r.subject.ref === sessionId,
  );
  if (mine.length === 0) return null;
  return orderResults(mine)[mine.length - 1] ?? null;
}

// ---- the store ---------------------------------------------------------------

interface ValidationStore {
  /** Held results, keyed by `validation_id`. Empty is a real answer (nothing
   * validated yet) and the common one — never a singleton. */
  results: Record<string, ValidationResult>;
  /**
   * `false` until the first `refresh()` settles (success *or* failure), `true`
   * forever after. Before it flips, an absent id is *unknown*, not gone — a
   * restored Review pane must wait for this rather than presume its still-held
   * result dead and flash a tombstone (product principle 4c: a restored pane is
   * vetted before it is believed). The usage/activity stores answer "is what I
   * hold real yet" the same way.
   */
  hydrated: boolean;
  init: () => void;
  refresh: () => Promise<void>;
  /** Run a validation (also drives it onto `/ws/events`); returns the result. */
  run: (spec: ValidationSpec) => Promise<ValidationResult>;
  /**
   * Record the human decision. Resolves with the updated result on success.
   * A stale/superseded id answers **404**: this throws the `ApiError` so the
   * panel can say "this result is no longer current" rather than read a failure
   * as an approval that landed, and it drops the dead entry from the map.
   */
  approve: (validationId: string, approver: string, note: string | null) => Promise<ValidationResult>;
  /**
   * Render this result as a one-page report and write it into the workspace.
   *
   * Resolves with the `EvidenceExport` — which carries both the markdown and the
   * path it landed at, so the caller can show either without a second call. A
   * 404 (evicted, superseded) throws the `ApiError`, exactly as `approve` does,
   * so nothing reads a failure as a report that was written.
   */
  exportResult: (validationId: string) => Promise<EvidenceExport>;
}

let started = false;

/** Reset the module-level "already subscribed" latch and the map. Tests only. */
export function resetValidationStoreForTests(): void {
  started = false;
  useValidationStore.setState({ results: {}, hydrated: false });
}

export const useValidationStore = create<ValidationStore>((set, get) => ({
  results: {},
  hydrated: false,

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "validation") return;
        set((state) => ({
          results: { ...state.results, [event.result.validation_id]: event.result },
        }));
      },
      // Every reconnect re-reads: a result created while the socket was down is
      // gone from the live stream, and the load path is the only way back to it.
      onOpen: () => {
        void get().refresh();
      },
    });
  },

  refresh: async () => {
    try {
      const { results } = await getValidations();
      set({ results: Object.fromEntries(results.map((r) => [r.validation_id, r])), hydrated: true });
    } catch {
      // A server that cannot answer leaves the last map standing — the honest
      // reading of the situation, same posture as usage/activity. Still counts as
      // hydrated: the store *answered*, so an absent id is now genuinely absent
      // and a restored pane may fall through to its tombstone.
      set({ hydrated: true });
    }
  },

  run: async (spec) => {
    // The event will also arrive on the socket; setting it here too means the
    // caller (the panel that issued the run) sees it without waiting a round the
    // bus, and the socket frame is an idempotent replace by the same id.
    const result = await runValidation(spec);
    set((state) => ({ results: { ...state.results, [result.validation_id]: result } }));
    return result;
  },

  approve: async (validationId, approver, note) => {
    const body: ApproveRequest = { approver, note };
    try {
      const updated = await approveValidation(validationId, body);
      set((state) => ({ results: { ...state.results, [updated.validation_id]: updated } }));
      return updated;
    } catch (err) {
      // 404 = the result was superseded/evicted while it sat on screen. Drop the
      // dead entry so the pane falls through to its tombstone, and re-throw so
      // the panel surfaces the stale message rather than a silent no-op.
      if (err instanceof ApiError && err.status === 404) {
        set((state) => {
          const { [validationId]: _gone, ...rest } = state.results;
          return { results: rest };
        });
      }
      throw err;
    }
  },

  exportResult: (validationId) => exportValidation(validationId),
}));
