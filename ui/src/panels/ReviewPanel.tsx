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

import { ApiError, getEvidencePayload } from "../api";
import { openPanel } from "../dock";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import type {
  CheckOutcome,
  EvidenceExport,
  EvidenceItem,
  EvidencePayload,
  ReadSource,
  ReviewSeverity,
  RiskLevel,
  ValidationResult,
} from "../types";
import {
  awaitingApproval,
  newestResult,
  orderResults,
  outcomeVisual,
  reviewCount,
  riskSeverity,
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

/** Severity → the outcome the row is tinted by, mirroring `SEVERITY_OUTCOME` in
 * `models/review.py`. Kept as the *display* mapping only: the grouped evidence
 * line's outcome is the server's, derived there and never recomputed here. */
const SEVERITY_OUTCOME: Record<ReviewSeverity, CheckOutcome> = {
  must_fix: "fail",
  should_fix: "warn",
  nit: "pass",
};

/** …and the word that leads each row, so colour is never the only signal. */
const SEVERITY_LABEL: Record<ReviewSeverity, string> = {
  must_fix: "must fix",
  should_fix: "should fix",
  nit: "nit",
};

/** Where a reconciliation's numbers came from, in one line a person reads.
 *
 * There are two readers now — the live Excel that has the workbook docked, and
 * the `.xlsx` on disk — and a green badge that does not say which is a colour
 * rather than proof. The server sends fields, not prose, so this is the one place
 * they become a sentence; `services/reconciliation.py::describe_source` writes
 * the same sentence into the evidence line for a reader who never expands the
 * table.
 *
 * Timestamps are naive local wall clock already (no offset, no zone), so they are
 * sliced rather than run through `Date`, which would attach one.
 */
export function sourceLine(source: ReadSource): string {
  if (source.kind === "live") {
    const clean = source.saved === true ? "workbook saved" : "workbook unsaved";
    return `Read live from the docked workbook at ${clockOf(source.read_at)}, calculation ${
      source.calculation ?? "unknown"
    }, ${clean}.`;
  }
  const when = source.mtime === null ? "unknown time" : source.mtime.replace("T", " ").slice(0, 19);
  const empty = source.cached_values === false ? " — the file cached no values" : "";
  return `Read from the file on disk, modified ${when}${empty}.`;
}

/** `2026-08-09T14:02:11` → `14:02:11`. */
const clockOf = (stamp: string): string => stamp.slice(11, 19) || stamp;

/** What the expander is showing right now. `evicted` is its own state, not an
 * error: the payload store is a bounded LRU and being dropped from it is the
 * honest, expected end of a log's life — never a spinner that never resolves. */
type PayloadState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; payload: EvidencePayload }
  | { phase: "evicted" }
  | { phase: "error" };

/** The bounded detail behind one `payload_ref`, fetched **lazily** — on the
 * first time its expander is opened, never on render. An index of a hundred
 * results must not be a hundred round trips for detail nobody asked to see.
 *
 * Exported so the states render under a static markup test rather than only in a
 * browser: a `gate` log, a `numeric` table, and the evicted 404.
 */
export function EvidencePayloadView({ state }: { state: PayloadState }) {
  if (state.phase === "loading") {
    return <p className="wb-evidence-payload">Loading the payload…</p>;
  }
  if (state.phase === "evicted") {
    return (
      <p className="wb-evidence-payload">
        This payload has been evicted — validation detail is held in a bounded in-memory
        store, so a busy server (or a restart) forgets the oldest. Re-run the validation to
        capture it again.
      </p>
    );
  }
  if (state.phase === "error") {
    return (
      <p className="wb-evidence-payload" role="alert">
        Could not load the payload. Try opening it again.
      </p>
    );
  }
  if (state.phase === "idle") return null;

  const { gate_log: log, reconciliation: report, review } = state.payload;
  if (review !== null) {
    return (
      <div className="wb-evidence-payload">
        <p className="u-tabular">
          {review.files_reviewed} file(s), {(review.diff_bytes / 1024).toFixed(1)} KB of diff
          against {review.base.slice(0, 12)} — {review.turns} turn(s), $
          {review.cost_usd.toFixed(2)}
        </p>
        {review.findings.length === 0 ? (
          // The explicit "none" (AXI shape 2). A review that found nothing and a
          // review that never ran must never look alike, and this is the half a
          // reader sees.
          <p className="wb-review-none">
            No findings — the reviewer read this change and could not break it.
          </p>
        ) : (
          <ul className="wb-evidence-rows">
            {review.findings.map((finding, index) => (
              <li
                key={`${finding.file ?? "change"}:${String(finding.line ?? index)}:${String(index)}`}
                data-outcome={SEVERITY_OUTCOME[finding.severity]}
              >
                {/* The severity word leads the row, so colour is never the only
                 * signal (DESIGN.md §7) — the tint only speeds up scanning. */}
                <strong>{SEVERITY_LABEL[finding.severity]}</strong>
                {finding.file === null ? (
                  ""
                ) : (
                  <span className="u-tabular">
                    {" "}
                    {finding.file}
                    {finding.line === null ? "" : `:${String(finding.line)}`}
                  </span>
                )}{" "}
                — {finding.claim}
                {/* The refutation is the reason to believe the claim, so it is
                 * shown rather than hidden behind another expander: a finding a
                 * reader cannot check is one they have to go and disprove. */}
                <div className="wb-evidence-detail">
                  Breaks when: {finding.refutation} ({finding.confidence})
                </div>
              </li>
            ))}
          </ul>
        )}
        {review.truncated !== null && (
          <p className="wb-evidence-truncation u-tabular">{review.truncated.detail}</p>
        )}
      </div>
    );
  }
  if (log !== null) {
    return (
      <div className="wb-evidence-payload">
        <p className="wb-evidence-argv u-tabular">
          {log.argv.join(" ")} —{" "}
          {log.exit_code === null ? "no exit code" : `exit ${String(log.exit_code)}`} in{" "}
          {(log.duration_ms / 1000).toFixed(1)}s
        </p>
        <pre className="wb-evidence-log">{log.text}</pre>
        {log.truncated !== null && (
          <p className="wb-evidence-truncation u-tabular">{log.truncated.detail}</p>
        )}
      </div>
    );
  }
  if (report !== null) {
    return (
      <div className="wb-evidence-payload">
        <p className="u-tabular">
          {report.matched} matched, {report.mismatched} mismatched of {report.total} —{" "}
          {report.workbook}
        </p>
        <p className="wb-evidence-source u-tabular">{sourceLine(report.source)}</p>
        <ul className="wb-evidence-rows">
          {report.comparisons.map((row) => (
            <li key={row.cell} className="u-tabular" data-outcome={row.outcome}>
              {row.cell}: expected {row.expected}
              {row.actual === null ? ", no value" : `, got ${String(row.actual)}`}
              {row.unit === "" ? "" : ` ${row.unit}`}
              {row.reason === null ? "" : ` — ${row.reason}`}
            </li>
          ))}
        </ul>
        {report.truncated !== null && (
          <p className="wb-evidence-truncation u-tabular">{report.truncated.detail}</p>
        )}
      </div>
    );
  }
  // A ref the server holds but this build has no shape for. Said out loud rather
  // than rendered as an empty box (AXI shape 2).
  return (
    <p className="wb-evidence-payload">
      This payload is a kind this version of the app cannot render.
    </p>
  );
}

/** One row: its label, its outcome pill, its detail line, and — when it names a
 * detail payload — an expander that redeems the ref.
 *
 * The #82 frame stored payloads and shipped no route to fetch them, so this
 * expander was a placeholder saying so. PR 1 closes that gap
 * (`GET /api/validation/payload/{kind}/{ref}`), because the *entire* value of a
 * failing gate is its captured output and a gate whose log a human cannot read
 * is a gate they have to take on faith.
 *
 * Numbers in the detail render in tabular figures (the DESIGN numeric rule). */
function EvidenceRow({ item }: { item: EvidenceItem }) {
  const [state, setState] = useState<PayloadState>({ phase: "idle" });
  const ref = item.payload_ref;

  const load = (): void => {
    if (ref === null || state.phase !== "idle") return;
    setState({ phase: "loading" });
    void getEvidencePayload(item.kind, ref)
      .then((payload) => setState({ phase: "ready", payload }))
      .catch((err: unknown) => {
        setState(
          err instanceof ApiError && err.status === 404 ? { phase: "evicted" } : { phase: "error" },
        );
      });
  };

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
      {ref !== null && (
        <details
          className="wb-evidence-expand"
          onToggle={(e) => {
            if (e.currentTarget.open) load();
          }}
        >
          <summary>Detail payload</summary>
          <EvidencePayloadView state={state} />
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

// ---- the export --------------------------------------------------------------

/** What the Export action is doing right now. `written` is the settled state and
 * it carries the whole report, so the panel can show the path *and* the document
 * without a second round trip. */
export type ExportState =
  | { phase: "idle" }
  | { phase: "running" }
  | { phase: "written"; report: EvidenceExport }
  | { phase: "error"; detail: string };

/**
 * Turn this result into something you can hand to somebody who was not there.
 *
 * The server renders **and writes** the report, so what this surfaces is a path
 * in the user's own workspace rather than a download the browser names. The
 * document itself is behind an expander: a one-page report is worth reading
 * before you send it, and it is not worth pushing the approval gate off screen.
 *
 * Pure over its props, so every state renders under a static markup test. Built
 * from classes `review.css` already defines — this PR adds no stylesheet, which
 * is why the report reuses the captured-log box the gate expander uses.
 */
export function ExportAction({
  state,
  onExport,
}: {
  state: ExportState;
  onExport: () => void;
}) {
  return (
    <div className="wb-review-approval wb-review-export">
      <button
        type="button"
        className="wb-btn wb-btn-outline wb-btn-sm wb-review-export-run"
        disabled={state.phase === "running"}
        onClick={onExport}
      >
        {state.phase === "running" ? "Exporting…" : "Export evidence…"}
      </button>
      {state.phase === "written" && (
        <>
          <p className="wb-evidence-detail u-tabular wb-review-export-path">
            Written to <code>{state.report.path}</code> ({state.report.bytes.toLocaleString()}{" "}
            bytes)
          </p>
          <details className="wb-evidence-expand">
            <summary>The report</summary>
            <pre className="wb-evidence-log">{state.report.markdown}</pre>
          </details>
        </>
      )}
      {state.phase === "error" && (
        <p className="wb-review-stale" role="alert">
          {state.detail}
        </p>
      )}
    </div>
  );
}

// ---- the whole review of one result -----------------------------------------

/** The badge, the subject, the summary, the gallery, the approval gate and the
 * export — everything one result says, and the one thing you can do with it
 * afterwards. Pure over props; the panel wires the store. */
export function ReviewView({
  result,
  note,
  pending,
  stale,
  exportState,
  onNote,
  onApprove,
  onExport,
}: {
  result: ValidationResult;
  note: string;
  pending: boolean;
  stale: string | null;
  exportState: ExportState;
  onNote: (value: string) => void;
  onApprove: () => void;
  onExport: () => void;
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
      <ExportAction state={exportState} onExport={onExport} />
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
  // Per pane, not per tool: two Review panes export two different results, and a
  // shared "last export" would show one pane the other pane's report.
  const [exportState, setExportState] = useState<ExportState>({ phase: "idle" });

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

  const onExport = (): void => {
    setExportState({ phase: "running" });
    void useValidationStore
      .getState()
      .exportResult(validationId)
      .then((report) => setExportState({ phase: "written", report }))
      .catch((err: unknown) => {
        setExportState({
          phase: "error",
          detail:
            err instanceof ApiError && err.status === 404
              ? "This result is no longer current — it was superseded or evicted. Re-run the validation."
              : "Could not write the report. Check that the workspace folder is writable.",
        });
      });
  };

  return (
    <ReviewView
      result={result}
      note={note}
      pending={pending}
      stale={stale}
      exportState={exportState}
      onNote={setNote}
      onApprove={onApprove}
      onExport={onExport}
    />
  );
}

export function ReviewPanel(props: IDockviewPanelProps) {
  const validationId = useMemo(() => paneInstance(props.api.id), [props.api.id]);
  const results = useValidationStore((s) => s.results);
  const hydrated = useValidationStore((s) => s.hydrated);

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
    // The tombstone is only honest once the store has *answered*. On a cold
    // launch that restores a pane bound to a still-held id, the first render
    // happens before the load resolves; a tombstone here would flash "re-run it"
    // over a live result. Wait for hydration, then fall through — presumed alive
    // until proven gone, never the inverse (product principle 4c).
    if (!hydrated) {
      return (
        <div className="wb-review">
          <p className="wb-review-loading">Loading validation…</p>
        </div>
      );
    }
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
  // The one severity table lives in `validation.ts` (`riskSeverity`); a second
  // copy here would silently drift from `isMediumOrWorse`/`reviewCount`.
  return [...results].sort((a, b) => riskSeverity(b.risk) - riskSeverity(a.risk));
}

// ---- registration ------------------------------------------------------------

/**
 * What one `validation.export` run actually did, in the shape the command relay
 * reports (`CommandOutcome`). Structural on purpose rather than imported: this
 * module owes the relay an answer, not a dependency on it.
 */
export interface ExportOutcome {
  ok: boolean;
  detail: string;
}

/**
 * `validation.export`, the command half of the export — reachable from the
 * QuickBar, from `shortcuts.md`, and from `workbench-cmd invoke`.
 *
 * It exports the **newest** result, because a parameterless command has to pick
 * one and the thing you just ran is overwhelmingly the thing you meant. Naming a
 * particular id is a *parameterised* invocation (`validation.export{validation_id}`),
 * which is a mechanism this repo does not have yet — so this is the first real
 * customer waiting for it rather than a second, private way of saying it.
 *
 * Both ends report through a toast: a command run from a CLI or a chord has no
 * pane to draw in, and a command that silently did or did not write a file is
 * the one thing a proof surface may not be. "Nothing validated yet" is said out
 * loud (AXI shape 2) rather than being a no-op that looks like a broken key.
 *
 * It **also returns that verdict**, and the two are not the same audience: the
 * toast is for whoever is looking at the window, the return value is for whoever
 * invoked it from outside one. `Command.run` is typed `() => void` today, so the
 * relay discards this — see the command's own note for what stands in until it
 * does not.
 *
 * The app store is reached through a **dynamic** `import()`, the `commandRelay.ts`
 * trick: a panel module has no business holding a load-time edge into `store.ts`
 * (which reaches the editor and the shell at import), and a command runs long
 * after the module graph has settled. It costs nothing — `store.ts` is already
 * in the entry chunk — and it is what keeps this module renderable in a
 * node-only test.
 */
export async function exportNewest(): Promise<ExportOutcome> {
  const store = useValidationStore.getState();
  const newest = newestResult(Object.values(store.results));
  const { useStore } = await import("../store");
  const toast = useStore.getState().pushToast;
  if (newest === null) {
    const detail = "Nothing to export yet — no validation has been run in this workspace.";
    toast("info", detail);
    return { ok: false, detail };
  }
  try {
    const report = await store.exportResult(newest.validation_id);
    const detail = `Evidence exported to ${report.path}`;
    toast("success", detail);
    return { ok: true, detail };
  } catch {
    const detail = "Could not export the evidence report.";
    toast("error", detail);
    return { ok: false, detail };
  }
}

/** Is there anything for `validation.export` to export right now? */
function hasExportableResult(): boolean {
  return newestResult(Object.values(useValidationStore.getState().results)) !== null;
}

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
    {
      id: "validation.export",
      title: "Export validation evidence",
      detail: () => {
        const newest = newestResult(Object.values(useValidationStore.getState().results));
        return newest === null ? "nothing validated yet" : `the newest: ${newest.subject.label}`;
      },
      // Safe from an untrusted `shortcuts.md` and from the CLI: it names no
      // path. The server picks the file name from the server-minted
      // `validation_id` and writes it under the workspace's own `.workbench/`,
      // so there is no input here through which a caller could reach a path.
      //
      // `when()` is what keeps the *external* answer honest, and it is here for
      // that rather than for the palette. `POST /api/commands/invoke` answers
      // `ok` = "whether that window then ran the command"
      // (`models/commands.py`), and `executeCommandById` can only get that from a
      // synchronous `run()` — so with nothing validated yet the caller used to be
      // told `ok: true` for a command whose whole body was a toast saying it had
      // done nothing. `when()` is the one channel the relay reads *before* it
      // runs (`commandRelay.ts`), and it turns that into an explicit refusal with
      // a reason, which is also what a `shortcuts.md` binding gets
      // (`commands.ts`) and what the QuickBar honours by not offering a command
      // that provably cannot act. The published manifest is unfiltered, so the
      // CLI still discovers the command and is told why, rather than never
      // hearing of it.
      when: hasExportableResult,
      // Returned, not `void`ed. `Command.run` is `() => void` today and the relay
      // discards it, so the *other* dishonest case — an export that starts and
      // then fails, which only ever reaches a toast — is still invisible to an
      // external caller. Fixing that means the relay awaiting a `run()` that can
      // answer, which is `ui/src/registry.ts` + `ui/src/commandRelay.ts`: PR-E's
      // files, and PR-E is retyping exactly that signature. Handing back the real
      // outcome here means that lands as an await and no change to this command.
      run: () => exportNewest(),
    },
  ],
  // No Alt chord by default (the Scratchpad/Workspaces precedent): a registered
  // chord is one the user's own `shortcuts.md` cannot have, and this is not a
  // reflex. It is bindable from `shortcuts.md` the day it registers.
  statusContributions: [{ region: "right", component: ReviewStatus }],
};
