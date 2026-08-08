/**
 * Objectives — the panel that binds a session to a goal and shows whether the
 * evidence has closed it (plan §3).
 *
 * A whole capability in one module (plus `objectives.ts` and its stylesheet): the
 * goal, its **evidence-derived** status badge, the evidence link, and the small
 * typed action that sets or clears it. It edits no shared file but the one line
 * in `tools.ts`.
 *
 * **Plural, and a view onto state it does not own.** One pane per named session,
 * bound by the session id — the pane's dockview instance key, which is the whole
 * of what `.workbench/layouts.json` persists. Two panes show two sessions' goals
 * side by side, independently. The goal text lives in `objectives.ts`' map; the
 * *status* is derived live from `validation.ts`' results, so an approval landing
 * on `/ws/events` flips this badge to `met` with no objective-specific socket —
 * the same result object the Review panel renders.
 *
 * **The badge reuses the existing risk vocabulary — no new colour.** The evidence
 * line renders the *same* `RiskBadge` the Review panel does; the status pill maps
 * onto the same semantic tokens (`objectives.ts`). Colour is never the only
 * signal — every pill carries its word (DESIGN.md §7).
 */

import type { IDockviewPanelProps } from "dockview";
import { memo, useEffect, useMemo, useState, type CSSProperties } from "react";

import { openPanel } from "../dock";
import { latestForObjective, objectiveStatus, objectiveStatusVisual, useObjectiveStore } from "../objectives";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import type { Objective, ObjectiveStatus, ValidationResult } from "../types";
import { useValidationStore } from "../validation";

import { revealPane } from "./Panes";
import { RiskBadge } from "./ReviewPanel";

import "../styles/review.css";
import "../styles/objective.css";

const TOOL_ID = "objective";

// ---- the status badge (§6.4, reuses the pill + the semantic ramp) -----------

/** The objective's status as a §6.4 status pill. Same markup and tokens as the
 * risk badge — it invents no colour, only maps the four statuses onto the ramp
 * (`objectives.ts`). Always carries its word; colour is never the only signal. */
export function ObjectiveBadge({ status }: { status: ObjectiveStatus }) {
  const visual = objectiveStatusVisual(status);
  const style = {
    "--pill-color": `var(${visual.token})`,
    "--pill-bg": `var(${visual.bg})`,
  } as CSSProperties;
  return (
    <span className="wb-pill" style={style} title={`Objective: ${visual.label}`}>
      <span className="wb-pill-dot" aria-hidden="true" />
      {visual.label}
    </span>
  );
}

// ---- the card: the goal, its status, and the evidence behind it -------------

/**
 * One objective, whole — pure over its props so every state renders under a
 * static test. A `met`/`at-risk`/`failing` status carries the evidence result it
 * was derived from, with the Review panel's own risk badge on it; an `open`
 * objective with no evidence says so rather than showing blankness (AXI shape 2).
 */
export function ObjectiveCard({
  objective,
  status,
  evidence,
}: {
  objective: Objective;
  status: ObjectiveStatus;
  evidence: ValidationResult | null;
}) {
  return (
    <section className="wb-objective-card" data-status={status}>
      <header className="wb-objective-head">
        <ObjectiveBadge status={status} />
      </header>
      <p className="wb-objective-goal">{objective.statement}</p>
      {objective.acceptance !== null && objective.acceptance !== "" && (
        <p className="wb-objective-acceptance">Done means: {objective.acceptance}</p>
      )}
      {evidence === null ? (
        <p className="wb-objective-noevidence">
          No evidence yet — validate this objective, then approve the result to close it.
        </p>
      ) : (
        <button
          type="button"
          className="wb-objective-evidence"
          title="Open this result in Review"
          onClick={() => revealPane("review", evidence.validation_id)}
        >
          <RiskBadge risk={evidence.risk} />
          <span className="wb-objective-evidence-label u-truncate">{evidence.summary}</span>
        </button>
      )}
    </section>
  );
}

// ---- the set/clear form (a small typed action) ------------------------------

function ObjectiveForm({
  objective,
  onSave,
  onClear,
}: {
  objective: Objective | null;
  onSave: (statement: string, acceptance: string | null) => Promise<void>;
  onClear: () => Promise<void>;
}) {
  const [statement, setStatement] = useState(objective?.statement ?? "");
  const [acceptance, setAcceptance] = useState(objective?.acceptance ?? "");
  const [pending, setPending] = useState(false);

  const save = (): void => {
    if (statement.trim() === "") return;
    setPending(true);
    void onSave(statement.trim(), acceptance.trim() === "" ? null : acceptance.trim()).finally(
      () => setPending(false),
    );
  };

  return (
    <div className="wb-objective-form">
      <label className="u-label" htmlFor="wb-objective-statement">
        Goal
      </label>
      <textarea
        id="wb-objective-statement"
        className="wb-objective-input"
        value={statement}
        placeholder="What is this session meant to achieve?"
        spellCheck={false}
        onChange={(e) => setStatement(e.target.value)}
      />
      <label className="u-label" htmlFor="wb-objective-acceptance">
        Acceptance (optional)
      </label>
      <textarea
        id="wb-objective-acceptance"
        className="wb-objective-input"
        value={acceptance}
        placeholder="Done means… (what a reviewer checks)"
        spellCheck={false}
        onChange={(e) => setAcceptance(e.target.value)}
      />
      <div className="wb-objective-actions">
        <button
          type="button"
          className="wb-btn wb-btn-primary wb-btn-sm"
          disabled={pending || statement.trim() === ""}
          onClick={save}
        >
          {objective === null ? "Set objective" : "Update objective"}
        </button>
        {objective !== null && (
          <button
            type="button"
            className="wb-btn wb-btn-outline wb-btn-sm"
            disabled={pending}
            onClick={() => void onClear()}
          >
            Clear
          </button>
        )}
      </div>
    </div>
  );
}

// ---- the panel: one session's objective, or an index ------------------------

function ObjectiveInstance({ sessionId }: { sessionId: string }) {
  const objective = useObjectiveStore((s) => s.objectives[sessionId]);
  const setGoal = useObjectiveStore((s) => s.set);
  const clearGoal = useObjectiveStore((s) => s.clear);
  const results = useValidationStore((s) => s.results);

  useEffect(() => {
    useValidationStore.getState().init();
    void useObjectiveStore.getState().load(sessionId);
  }, [sessionId]);

  const latest = latestForObjective(Object.values(results), sessionId);
  const current = objective ?? null;
  const status = objectiveStatus(current, latest);

  return (
    <div className="wb-objective-body" data-session={sessionId} data-status={status}>
      {current !== null && (
        <ObjectiveCard objective={current} status={status} evidence={latest} />
      )}
      <ObjectiveForm
        objective={current}
        onSave={(statement, acceptance) => setGoal(sessionId, statement, acceptance)}
        onClear={() => clearGoal(sessionId)}
      />
    </div>
  );
}

/** The bare `objective` pane: an index of the sessions that carry a goal, each a
 * row that opens *its* objective. An empty index says so — a quiet, designed
 * state, not a broken panel. */
function ObjectiveIndex() {
  const sessions = useObjectiveStore((s) => s.sessions);
  const objectives = useObjectiveStore((s) => s.objectives);
  const results = useValidationStore((s) => s.results);

  useEffect(() => {
    useValidationStore.getState().init();
    // Refresh, not just init: the status-bar reading may have latched the store
    // at app start, before any named session existed — opening the index must
    // re-read the list rather than trust a snapshot taken when it was empty.
    void useObjectiveStore.getState().refresh();
  }, []);

  const withGoals = sessions.filter((s) => (objectives[s.id] ?? s.objective ?? null) !== null);
  if (withGoals.length === 0) {
    return (
      <div className="wb-objective-empty">
        <span>
          No objectives yet. Open a session and set a goal — its status is closed by a
          validated, approved result, not by opinion.
        </span>
      </div>
    );
  }
  return (
    <ul className="wb-objective-index">
      {withGoals.map((session) => {
        const goal = objectives[session.id] ?? session.objective ?? null;
        const status = objectiveStatus(
          goal,
          latestForObjective(Object.values(results), session.id),
        );
        return (
          <li key={session.id}>
            <button
              type="button"
              className="wb-objective-row"
              data-session={session.id}
              onClick={() => revealPane(TOOL_ID, session.id)}
            >
              <ObjectiveBadge status={status} />
              <span className="wb-objective-row-goal u-truncate">
                {goal?.statement ?? session.name}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export function ObjectivePanel(props: IDockviewPanelProps) {
  const sessionId = useMemo(() => paneInstance(props.api.id), [props.api.id]);
  if (sessionId === null) {
    return (
      <div className="wb-objective">
        <ObjectiveIndex />
      </div>
    );
  }
  return (
    <div className="wb-objective">
      <ObjectiveInstance sessionId={sessionId} />
    </div>
  );
}

// ---- the status-bar reading (§6.7, hides at zero) ---------------------------

/** How many objectives the evidence says are not on track — `at-risk` or
 * `failing`. Hidden at zero (a quiet bar means nothing needs you, §6.7). */
export function offTrackCount(
  sessions: readonly { id: string; objective?: Objective | null }[],
  objectives: Readonly<Record<string, Objective | null>>,
  results: readonly ValidationResult[],
): number {
  let count = 0;
  for (const session of sessions) {
    const goal = objectives[session.id] ?? session.objective ?? null;
    if (goal === null) continue;
    const status = objectiveStatus(goal, latestForObjective(results, session.id));
    if (status === "at-risk" || status === "failing") count += 1;
  }
  return count;
}

export const ObjectiveStatusReading = memo(function ObjectiveStatusReading() {
  const sessions = useObjectiveStore((s) => s.sessions);
  const objectives = useObjectiveStore((s) => s.objectives);
  const results = useValidationStore((s) => s.results);
  useEffect(() => {
    useValidationStore.getState().init();
    useObjectiveStore.getState().init();
  }, []);
  const count = offTrackCount(sessions, objectives, Object.values(results));
  if (count === 0) return null;
  return (
    <button
      type="button"
      className="wb-objective-status"
      title={`${String(count)} objective${count === 1 ? "" : "s"} off track`}
      onClick={() => openPanel(TOOL_ID)}
    >
      <span aria-hidden="true">◎</span>
      <span className="u-tabular">{count}</span>
      <span>off track</span>
    </button>
  );
});

// ---- registration ------------------------------------------------------------

function ObjectiveIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="6.4" />
      <circle cx="8" cy="8" r="3" />
      <circle cx="8" cy="8" r="0.6" fill="currentColor" />
    </svg>
  );
}

export const objectiveTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Objectives",
  icon: ObjectiveIcon,
  panel: {
    component: ObjectivePanel,
    defaultLocation: { area: "right", size: 380 },
    // Opened on demand — a goal is something you go to set or check on, not a
    // pane that takes room from your work. The status reading is always there.
    openByDefault: false,
    // Plural: one pane per session, bound by the session id.
    singleton: false,
    instances: {
      options: () => {
        const { sessions, objectives } = useObjectiveStore.getState();
        return sessions.map((session) => ({
          id: `objective.${session.id}`,
          title: session.name,
          detail: (objectives[session.id] ?? session.objective ?? null)?.statement ?? "no goal yet",
          category: "Objectives",
          key: () => session.id,
        }));
      },
      titleFor: (key) => {
        const { sessions } = useObjectiveStore.getState();
        return sessions.find((session) => session.id === key)?.name ?? "Objective";
      },
    },
  },
  commands: [
    {
      id: "objective.open",
      title: "Objectives…",
      detail: () => "bind a session to a goal, closed by evidence",
      run: () => openPanel(TOOL_ID),
    },
  ],
  statusContributions: [{ region: "right", component: ObjectiveStatusReading }],
};
