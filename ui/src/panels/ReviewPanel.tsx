/**
 * Review — the panel that *surfaces* validation (M6 PR3).
 *
 * A whole capability in one module (plus `validation.ts` and its stylesheet):
 * the evidence gallery, the risk badge, the human approval gate, the status-bar
 * reading and the command that opens it. It edits no shared file but the one
 * line in `tools.ts`.
 *
 * **Plural, and a view onto a result it does not own.** One pane per
 * `ValidationResult`, bound by its `validation_id` — the pane's dockview id,
 * which is the whole of what `.workbench/layouts.json` persists (`../panes.ts`).
 * So a saved arrangement brings the pane back pointed at the *same* result, and
 * two Review panes review two subjects side by side, independently. The result
 * itself lives in `validation.ts`' map keyed by that same id — this pane reads
 * it, never holds it.
 *
 * **A restored pane is vetted before it is believed.** Results are in-memory and
 * LRU-bounded server-side; a restart forgets them, and a pane saved onto a
 * result the server no longer holds renders a named tombstone with the one
 * recovery (re-run it) rather than a dead pane (product principle 4c).
 *
 * The badge maps straight onto the existing semantic ramp — **no new colour**
 * (see `validation.ts`): `pass → --success`, `low → --info`, `medium → --warn`,
 * `high → --error`, `blocked → --agent-idle`. Its dot-only variant carries an
 * `aria-label`, because colour is never the only signal (DESIGN.md §7).
 */

import type { IDockviewPanelProps } from "dockview";
import { memo, useEffect, useMemo, useState, type CSSProperties } from "react";

import { ApiError } from "../api";
import { openPanel } from "../dock";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import type { CheckOutcome, EvidenceItem, RiskLevel, ValidationResult } from "../types";
import {
  awaitingApproval,
  orderResults,
  outcomeVisual,
  reviewCount,
  riskVisual,
  useValidationStore,
} from "../validation";

import { revealPane } from "./Panes";

import "../styles/review.css";

const TOOL_ID = "review";

/** The human recorded on an approval. A local, single-user workbench has one
 * reviewer — you — so this is a constant rather than an identity lookup the app
 * does not otherwise have. The server stamps the timestamp. */
const APPROVER = "you";

// ---- the badge + the pill (§6.4) --------------------------------------------

/**
 * The risk badge, a §6.4 status pill. `dotOnly` is the tab-strip / Mission
 * Control variant — a 10px dot that still carries its `aria-label`, because a
 * colour with no word is no signal at all. Exported so the Mission Control card
 * and any other reader render the *same* badge from the *same* result object.
 */
export function RiskBadge({ risk, dotOnly = false }: { risk: RiskLevel; dotOnly?: boolean }) {
  const visual = riskVisual(risk);
  const style = { "--pill-color": `var(${visual.token})`, "--pill-bg": `var(${visual.bg})` } as CSSProperties;
  if (dotOnly) {
    return (
      <span
        className="wb-pill-dotonly"
        style={style}
        role="img"
        aria-label={`Validation risk: ${visual.label}`}
      />
    );
  }
  return (
    <span className="wb-pill" style={style} title={`Validation risk: ${visual.label}`}>
      <span className="wb-pill-dot" aria-hidden="true" />
      {visual.label}
    </span>
  );
}

/** One evidence line's outcome, a §6.4 status pill (label always present). */
export function OutcomePill({ outcome }: { outcome: CheckOutcome }) {
  const visual = outcomeVisual(outcome);
  const style = { "--pill-color": `var(${visual.token})`, "--pill-bg": `var(${visual.bg})` } as CSSProperties;
  return (
    <span className="wb-pill" style={style}>
      <span className="wb-pill-dot" aria-hidden="true" />
      {visual.label}
    </span>
  );
}

// ---- the evidence gallery ----------------------------------------------------

/** One row: its label, its outcome pill, its detail line, and — when it names a
 * detail payload — an expand.
 *
 * #82 exposes **no payload endpoint**, so the expand shows the inline detail and
 * says the bounded payload view (the reconciliation table, a captured log) is
 * deferred until that endpoint ships. We do not invent a server route here.
 * Numbers in the detail render in tabular figures (the DESIGN numeric rule). */
function EvidenceRow({ item }: { item: EvidenceItem }) {
  return (
    <li className="wb-evidence" data-kind={item.kind} data-outcome={item.outcome}>
      <div className="wb-evidence-head">
        <OutcomePill outcome={item.outcome} />
        <span className="wb-evidence-label u-truncate" title={item.label}>
          {item.label}
        </span>
        <span className="wb-evidence-kind">{item.kind}</span>
      </div>
      <p className="wb-evidence-detail u-tabular">{item.detail}</p>
      {item.payload_ref !== null && (
        <details className="wb-evidence-expand">
          <summary>Detail payload</summary>
          <p className="wb-evidence-payload u-tabular">
            The bounded payload view (ref {item.payload_ref}) arrives with the payload
            endpoint — deferred past this PR. The line above is the whole of what #82
            carries inline.
          </p>
        </details>
      )}
    </li>
  );
}

/**
 * The gallery: one row per `EvidenceItem`. An empty gallery **says so** rather
 * than showing blankness (AXI shape 2) — a `blocked` result has no evidence, and
 * the summary above it is where the *why* lives. A truncated evidence list names
 * the cut and how to widen it (AXI shape 1), straight off the server's own
 * `truncated`.
 */
export function EvidenceGallery({ result }: { result: ValidationResult }) {
  if (result.evidence.length === 0) {
    return (
      <p className="wb-review-none">
        No evidence — nothing was judged. See the summary above for why.
      </p>
    );
  }
  return (
    <>
      <ul className="wb-review-gallery">
        {result.evidence.map((item, index) => (
          <EvidenceRow key={`${item.kind}:${item.label}:${String(index)}`} item={item} />
        ))}
      </ul>
      {result.truncated !== null && (
        <p className="wb-review-truncation">{result.truncated.detail}</p>
      )}
    </>
  );
}

// ---- the approval gate -------------------------------------------------------

/**
 * The one mandatory human decision. Pure over its props so every state renders
 * under a static markup test:
 *
 * - already approved → who and when, settled;
 * - `medium`-or-worse and unapproved → *awaiting review*, with a note field and
 *   the amber Approve action (the one action the app is blocked on, §2.4);
 * - a `stale` message when the last approve answered 404 (superseded/evicted) —
 *   surfaced as "no longer current", never read as an approval that landed;
 * - `pass`/`low` → nothing; no approval is owed.
 */
export function ApprovalGate({
  result,
  note,
  pending,
  stale,
  onNote,
  onApprove,
}: {
  result: ValidationResult;
  note: string;
  pending: boolean;
  stale: string | null;
  onNote: (value: string) => void;
  onApprove: () => void;
}) {
  if (result.approval !== null) {
    const when = new Date(result.approval.timestamp).toLocaleString();
    return (
      <div className="wb-review-approval">
        <p className="wb-review-approved">
          Approved by {result.approval.approver} · <span className="u-tabular">{when}</span>
          {result.approval.note !== null && result.approval.note !== "" && (
            <> — “{result.approval.note}”</>
          )}
        </p>
      </div>
    );
  }
  if (!awaitingApproval(result)) return null;
  return (
    <div className="wb-review-approval">
      <p className="wb-review-awaiting">Awaiting approval — a human must clear this result.</p>
      <textarea
        className="wb-review-note"
        value={note}
        placeholder="Optional note recorded with your decision"
        spellCheck={false}
        aria-label="Approval note"
        onChange={(e) => onNote(e.target.value)}
      />
      <button
        type="button"
        className="wb-review-approve"
        disabled={pending}
        onClick={onApprove}
      >
        {pending ? "Approving…" : "Approve"}
      </button>
      {stale !== null && (
        <p className="wb-review-stale" role="alert">
          {stale}
        </p>
      )}
    </div>
  );
}

// ---- the whole review of one result -----------------------------------------

/** The badge, the subject, the summary, the gallery and the approval gate —
 * everything one result says. Pure over props; the panel wires the store. */
export function ReviewView({
  result,
  note,
  pending,
  stale,
  onNote,
  onApprove,
}: {
  result: ValidationResult;
  note: string;
  pending: boolean;
  stale: string | null;
  onNote: (value: string) => void;
  onApprove: () => void;
}) {
  return (
    <section className="wb-review-body" data-risk={result.risk} data-validation={result.validation_id}>
      <header className="wb-review-head">
        <RiskBadge risk={result.risk} />
        <span className="wb-review-subject u-truncate" title={result.subject.label}>
          {result.subject.label}
        </span>
      </header>
      <p className="wb-review-summary">{result.summary}</p>
      <EvidenceGallery result={result} />
      <ApprovalGate
        result={result}
        note={note}
        pending={pending}
        stale={stale}
        onNote={onNote}
        onApprove={onApprove}
      />
    </section>
  );
}

// ---- the panel: index, one result, or a tombstone ---------------------------

/** A pane bound to a result the fleet no longer holds (a restart forgets them,
 * or the LRU evicted it). Never an empty pane. */
function ReviewTombstone({ id, api }: { id: string; api: IDockviewPanelProps["api"] }) {
  return (
    <div className="wb-review-tombstone">
      <span>
        This result is no longer loaded — validations are held in memory and a restart (or
        the LRU) forgets them. Re-run the validation to review it again.
      </span>
      <button type="button" className="wb-btn wb-btn-outline wb-btn-sm" onClick={() => api.close()}>
        Close
      </button>
      <span className="u-label">{id}</span>
    </div>
  );
}

/** The bare `review` pane: an index of every held result, newest last, each a
 * row that opens *its* result in an instance pane. An empty index says so — a
 * quiet, designed state, not a broken panel. */
function ReviewIndex({ results }: { results: ValidationResult[] }) {
  if (results.length === 0) {
    return (
      <div className="wb-review-empty">
        <span>No validations yet. When an agent's output or a workbook is validated, its
          result shows here — with its risk, its evidence, and the approval it may need.</span>
      </div>
    );
  }
  return (
    <ul className="wb-review-index">
      {results.map((result) => (
        <li key={result.validation_id}>
          <button
            type="button"
            className="wb-review-row"
            data-validation={result.validation_id}
            onClick={() => revealPane(TOOL_ID, result.validation_id)}
          >
            <RiskBadge risk={result.risk} dotOnly />
            <span className="wb-review-row-subject u-truncate" title={result.subject.label}>
              {result.subject.label}
            </span>
            {awaitingApproval(result) && <span className="u-label">awaiting</span>}
          </button>
        </li>
      ))}
    </ul>
  );
}

/** One result's review, with the note field and the approve round trip wired to
 * the store. A 404 on approve becomes the "no longer current" message rather
 * than a success (`validation.ts` re-throws it). */
function ReviewInstance({ validationId }: { validationId: string }) {
  const result = useValidationStore((s) => s.results[validationId]);
  const approve = useValidationStore((s) => s.approve);
  const [note, setNote] = useState("");
  const [pending, setPending] = useState(false);
  const [stale, setStale] = useState<string | null>(null);

  if (result === undefined) {
    // The store answered but has no such id — a restored pane whose result is
    // gone. The tombstone (rendered by the panel) is the honest view; here we
    // signal it by returning null and letting the panel decide.
    return null;
  }

  const onApprove = (): void => {
    setPending(true);
    setStale(null);
    void approve(validationId, APPROVER, note === "" ? null : note)
      .catch((err: unknown) => {
        setStale(
          err instanceof ApiError && err.status === 404
            ? "This result is no longer current — it was superseded or evicted. Re-run the validation."
            : "Could not record the approval. Try again.",
        );
      })
      .finally(() => setPending(false));
  };

  return (
    <ReviewView
      result={result}
      note={note}
      pending={pending}
      stale={stale}
      onNote={setNote}
      onApprove={onApprove}
    />
  );
}

export function ReviewPanel(props: IDockviewPanelProps) {
  const validationId = useMemo(() => paneInstance(props.api.id), [props.api.id]);
  const results = useValidationStore((s) => s.results);

  useEffect(() => {
    // The panel is a reader of the validation service; opening it is what starts
    // the replay + subscription (the Mission Control precedent).
    useValidationStore.getState().init();
  }, []);

  if (validationId === null) {
    return (
      <div className="wb-review">
        <ReviewIndex results={orderResults(Object.values(results))} />
      </div>
    );
  }
  if (results[validationId] === undefined) {
    return (
      <div className="wb-review">
        <ReviewTombstone id={validationId} api={props.api} />
      </div>
    );
  }
  return (
    <div className="wb-review">
      <ReviewInstance validationId={validationId} />
    </div>
  );
}

// ---- the status-bar reading (§6.7, hides at zero) ---------------------------

/** A count of subjects at `medium`-or-worse awaiting review. Hidden at zero —
 * a quiet bar means nothing needs you (§6.7). Opens the Review index. */
export const ReviewStatus = memo(function ReviewStatus() {
  const results = useValidationStore((s) => s.results);
  useEffect(() => {
    useValidationStore.getState().init();
  }, []);
  const count = reviewCount(Object.values(results));
  if (count === 0) return null;
  return (
    <button
      type="button"
      className="wb-review-status"
      title={`${String(count)} validation${count === 1 ? "" : "s"} awaiting review`}
      onClick={() => openPanel(TOOL_ID)}
    >
      <span aria-hidden="true">⚠</span>
      <span className="u-tabular">{count}</span>
      <span>to review</span>
    </button>
  );
});

/** The tab badge: a dot-only risk badge for the worst result still awaiting
 * review, or nothing. This is the "background tab" surface for the badge — a
 * state worth seeing while the panel is behind another (§6.4). */
export const ReviewTabBadge = memo(function ReviewTabBadge() {
  const results = useValidationStore((s) => s.results);
  const worst = worstAwaiting(Object.values(results));
  if (worst === null) return null;
  return <RiskBadge risk={worst} dotOnly />;
});

/** The most severe risk among the results still awaiting review, or null. */
export function worstAwaiting(results: readonly ValidationResult[]): RiskLevel | null {
  const awaiting = results.filter(awaitingApproval);
  if (awaiting.length === 0) return null;
  return orderBySeverity(awaiting)[0]?.risk ?? null;
}

function orderBySeverity(results: readonly ValidationResult[]): ValidationResult[] {
  const severity: Record<RiskLevel, number> = { pass: 0, low: 1, medium: 2, high: 3, blocked: 4 };
  return [...results].sort((a, b) => severity[b.risk] - severity[a.risk]);
}

// ---- registration ------------------------------------------------------------

function ReviewIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3 8.5 6.5 12 13 4" />
      <circle cx="8" cy="8" r="6.4" />
    </svg>
  );
}

export const reviewTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Review",
  icon: ReviewIcon,
  panel: {
    component: ReviewPanel,
    defaultLocation: { area: "right", size: 420 },
    // Opened on demand — a review is something you go to look at, not a pane that
    // takes room from your work. The status reading is always there.
    openByDefault: false,
    // Plural: one pane per validation result, bound by `validation_id`.
    singleton: false,
    badge: ReviewTabBadge,
    instances: {
      options: () =>
        orderResults(Object.values(useValidationStore.getState().results)).map((result) => ({
          id: `review.${result.validation_id}`,
          title: result.subject.label,
          detail: `${result.risk} · review this result`,
          category: "Validations",
          key: () => result.validation_id,
        })),
      // A restored pane whose result is gone still reads as something — its id —
      // rather than a raw pane id; a live one reads by its subject label.
      titleFor: (key) => {
        const result = useValidationStore.getState().results[key];
        return result === undefined ? key : result.subject.label;
      },
    },
  },
  commands: [
    {
      id: "review.open",
      title: "Review validation…",
      detail: () => "the evidence, the risk badge, and the approval gate",
      run: () => openPanel(TOOL_ID),
    },
  ],
  // No Alt chord by default (the Scratchpad/Workspaces precedent): a registered
  // chord is one the user's own `shortcuts.md` cannot have, and this is not a
  // reflex. It is bindable from `shortcuts.md` the day it registers.
  statusContributions: [{ region: "right", component: ReviewStatus }],
};
