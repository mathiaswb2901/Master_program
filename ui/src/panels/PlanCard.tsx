/**
 * Native rendering of a `PlanArtifact` (M4 visual plan artifacts).
 *
 * The agent sends typed nodes, never markup: option groups become real radio
 * groups, step lists become ordered steps whose file refs open editor tabs,
 * questions become inputs. The user's clicks go back to the agent as a typed
 * `PlanResponse`.
 *
 * DESIGN.md: accent is spent only on the recommended option and Approve
 * (principle 3); Reject is a ghost button, never a red fill (§6.3); every
 * control is keyboard operable with a visible focus ring (§7).
 */

import { useId, useState } from "react";

import { Markdown } from "../markdown";
import { emptyPlanDraft, unchosenOptionGroups, useStore, type PlanDraft } from "../store";
import type { OptionGroupNode, PlanArtifact, PlanNode, PlanVerdict, StepListNode } from "../types";
import { VisualView } from "../visual/Visual";

/** Server caps (models/plans.py) — enforced here so a decision never bounces. */
const MAX_ANNOTATION = 600;
const MAX_COMMENT = 2000;

/** Stable identity: a fresh object per render would re-render on every store tick. */
const NO_DRAFT: PlanDraft = emptyPlanDraft();

const VERDICT_LABEL: Record<PlanVerdict, string> = {
  approve: "Approved",
  revise: "Revision requested",
  reject: "Rejected",
  no_decision: "No decision",
};

function FileChip({ path }: { path: string }) {
  return (
    <button
      type="button"
      className="wb-plan-file"
      title={`Open ${path}`}
      aria-label={`Open ${path}`}
      onClick={() => void useStore.getState().openFile(path)}
    >
      {path}
    </button>
  );
}

function Annotation({
  planId,
  nodeId,
  label,
  placeholder,
  value,
  readOnly,
}: {
  planId: string;
  nodeId: string;
  label: string;
  placeholder: string;
  value: string;
  readOnly: boolean;
}) {
  const id = useId();
  if (readOnly) {
    return value === "" ? null : (
      <div className="wb-plan-note-read">
        <span className="u-label">{label}</span>
        <span>{value}</span>
      </div>
    );
  }
  return (
    <div className="wb-plan-note">
      <label className="u-label" htmlFor={id}>
        {label}
      </label>
      <textarea
        id={id}
        rows={2}
        value={value}
        maxLength={MAX_ANNOTATION}
        placeholder={placeholder}
        spellCheck={false}
        onChange={(e) => useStore.getState().setPlanAnnotation(planId, nodeId, e.target.value)}
      />
    </div>
  );
}

function OptionGroup({
  planId,
  node,
  draft,
  readOnly,
}: {
  planId: string;
  node: OptionGroupNode;
  draft: PlanDraft;
  readOnly: boolean;
}) {
  const chosen = draft.choices[node.node_id];
  return (
    <fieldset className="wb-plan-group">
      <legend className="wb-plan-prompt">{node.prompt}</legend>
      <div className="wb-plan-options">
        {node.options.map((option) => {
          const selected = chosen === option.option_id;
          return (
            <label
              key={option.option_id}
              className={
                "wb-plan-option" +
                (selected ? " is-selected" : "") +
                (option.recommended ? " is-recommended" : "")
              }
            >
              <input
                className="u-sr-only"
                type="radio"
                name={`plan-${planId}-${node.node_id}`}
                value={option.option_id}
                checked={selected}
                disabled={readOnly}
                onChange={() =>
                  useStore.getState().setPlanChoice(planId, node.node_id, option.option_id)
                }
              />
              <span className="wb-plan-option-head">
                <span className="wb-plan-option-label">{option.label}</span>
                {option.recommended && <span className="wb-plan-rec">Recommended</span>}
                {readOnly && selected && <span className="wb-plan-chosen">Chosen</span>}
              </span>
              {(option.pros.length > 0 || option.cons.length > 0) && (
                <ul className="wb-plan-tradeoffs">
                  {option.pros.map((pro, i) => (
                    <li key={`p${i}`}>
                      <span className="wb-plan-sign" aria-hidden="true">
                        +
                      </span>
                      <span className="u-sr-only">Pro: </span>
                      {pro}
                    </li>
                  ))}
                  {option.cons.map((con, i) => (
                    <li key={`c${i}`}>
                      <span className="wb-plan-sign" aria-hidden="true">
                        −
                      </span>
                      <span className="u-sr-only">Con: </span>
                      {con}
                    </li>
                  ))}
                </ul>
              )}
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function StepList({ node }: { node: StepListNode }) {
  return (
    <ol className="wb-plan-steps">
      {node.steps.map((step, i) => (
        <li key={i}>
          <span className="wb-plan-step-text">{step.text}</span>
          {step.file_refs.length > 0 && (
            <span className="wb-plan-files">
              {step.file_refs.map((ref) => (
                <FileChip key={ref.path} path={ref.path} />
              ))}
            </span>
          )}
        </li>
      ))}
    </ol>
  );
}

function NodeView({
  planId,
  node,
  draft,
  readOnly,
}: {
  planId: string;
  node: PlanNode;
  draft: PlanDraft;
  readOnly: boolean;
}) {
  const note = draft.annotations[node.node_id] ?? "";
  switch (node.kind) {
    case "markdown":
      return (
        <div className="wb-plan-node">
          <Markdown text={node.text} />
        </div>
      );
    case "option_group":
      return (
        <div className="wb-plan-node">
          <OptionGroup planId={planId} node={node} draft={draft} readOnly={readOnly} />
          <NoteToggle planId={planId} nodeId={node.node_id} value={note} readOnly={readOnly} />
        </div>
      );
    case "step_list":
      return (
        <div className="wb-plan-node">
          <StepList node={node} />
          <NoteToggle planId={planId} nodeId={node.node_id} value={note} readOnly={readOnly} />
        </div>
      );
    case "visual":
      return (
        <div className="wb-plan-node">
          <VisualView node={node} />
          <NoteToggle planId={planId} nodeId={node.node_id} value={note} readOnly={readOnly} />
        </div>
      );
    case "question":
      return (
        <div className="wb-plan-node">
          <div className="wb-plan-question">{node.text}</div>
          <Annotation
            planId={planId}
            nodeId={node.node_id}
            label="Your answer"
            placeholder="Answer the agent's question"
            value={note}
            readOnly={readOnly}
          />
        </div>
      );
  }
}

/** Per-node note: hidden behind one ghost button until asked for, so an
 * unannotated plan stays as quiet as the design wants it. */
function NoteToggle({
  planId,
  nodeId,
  value,
  readOnly,
}: {
  planId: string;
  nodeId: string;
  value: string;
  readOnly: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (readOnly || open || value !== "") {
    return (
      <Annotation
        planId={planId}
        nodeId={nodeId}
        label="Note"
        placeholder="Note for the agent about this part"
        value={value}
        readOnly={readOnly}
      />
    );
  }
  return (
    <button type="button" className="wb-btn wb-btn-ghost wb-btn-sm" onClick={() => setOpen(true)}>
      Add note
    </button>
  );
}

export function PlanCard({ plan }: { plan: PlanArtifact }) {
  const draft = useStore((s) => s.plans[plan.plan_id] ?? NO_DRAFT);
  const titleId = useId();
  const commentId = useId();
  const settled = draft.verdict !== null;
  const decide = (verdict: Exclude<PlanVerdict, "no_decision">): void =>
    useStore.getState().decidePlan(plan.plan_id, verdict);
  // Approving with a group unanswered would be an implied approval of a choice
  // the user never made — the agent would just guess. Revise/Reject need no
  // choices, so only Approve waits.
  const unchosen = unchosenOptionGroups(plan, draft);
  const hintId = `${titleId}-approve-hint`;

  return (
    <section className="wb-plan-card" aria-labelledby={titleId}>
      <header className="wb-plan-head">
        <span className="u-label">Plan</span>
        <h3 className="wb-plan-title" id={titleId}>
          {plan.title}
        </h3>
        {draft.verdict !== null && (
          <span className="wb-plan-verdict">{VERDICT_LABEL[draft.verdict]}</span>
        )}
      </header>
      {plan.summary !== "" && <p className="wb-plan-summary">{plan.summary}</p>}
      <div className="wb-plan-body">
        {plan.nodes.map((node) => (
          <NodeView
            key={node.node_id}
            planId={plan.plan_id}
            node={node}
            draft={draft}
            readOnly={settled}
          />
        ))}
      </div>
      {settled ? (
        draft.comment !== "" && (
          <div className="wb-plan-note-read">
            <span className="u-label">Comment</span>
            <span>{draft.comment}</span>
          </div>
        )
      ) : (
        <div className="wb-plan-footer">
          <label className="u-label" htmlFor={commentId}>
            Comment
          </label>
          <textarea
            id={commentId}
            rows={2}
            value={draft.comment}
            maxLength={MAX_COMMENT}
            placeholder="Anything the agent should know before starting"
            spellCheck={false}
            onChange={(e) => useStore.getState().setPlanComment(plan.plan_id, e.target.value)}
          />
          <div className="wb-plan-actions">
            <button
              type="button"
              className="wb-btn wb-btn-primary"
              disabled={unchosen.length > 0}
              aria-describedby={unchosen.length > 0 ? hintId : undefined}
              onClick={() => decide("approve")}
            >
              Approve
            </button>
            <button type="button" className="wb-btn wb-btn-outline" onClick={() => decide("revise")}>
              Revise
            </button>
            <button
              type="button"
              className="wb-btn wb-btn-ghost wb-btn-deny"
              onClick={() => decide("reject")}
            >
              Reject
            </button>
            {unchosen.length > 0 && (
              <span className="wb-plan-hint" id={hintId} role="note">
                {unchosen.length === 1
                  ? "Pick an option above to approve"
                  : `Pick an option in ${unchosen.length} groups above to approve`}
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
