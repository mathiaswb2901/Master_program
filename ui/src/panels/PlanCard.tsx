/**
 * Native rendering of a `PlanArtifact` (M4 visual plan artifacts).
 *
 * The agent sends typed nodes, never markup: option groups become real radio
 * groups, step lists become ordered steps whose file refs open editor tabs,
 * questions become inputs. The user's clicks go back to the agent as a typed
 * `PlanResponse`.
 *
 * **Annotate mode** (M5, PR 3) is the second half of that idea. A card is where
 * a decision is made, and the thing you want to say about a plan is almost
 * never about the whole plan — it is about *that* number, *that* box, *that*
 * line. In annotate mode every anchorable part of the card becomes a real
 * button; picking one opens a note attached to exactly that part, addressed by
 * a semantic path (`../plan/anchors.ts`), and every note travels with the
 * verdict in the same single `PlanResponse` the card always sent. There is no
 * second channel to the agent — sending notes *without* deciding is live
 * refinement, and that is PR 5.
 *
 * DESIGN.md: accent is spent only on the recommended option and Approve
 * (principle 3); Reject is a ghost button, never a red fill (§6.3); every
 * control is keyboard operable with a visible focus ring (§7) — which is why
 * annotate mode is a chord, a QuickBar command and a tab order, not a click
 * target.
 */

import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";

import { Markdown } from "../markdown";
import {
  anchorKey,
  anchorLabel,
  nodeAnchor,
  PLAN_ANCHOR,
  rangeAnchor,
  textSegments,
} from "../plan/anchors";
import type { ToolCommand } from "../registry";
import {
  emptyPlanDraft,
  noteText,
  pendingPlanId,
  unchosenOptionGroups,
  useStore,
  type AnchoredNote,
  type PlanDraft,
} from "../store";
import type {
  AnnotationAnchor,
  MarkdownNode,
  OptionGroupNode,
  PlanArtifact,
  PlanNode,
  PlanVerdict,
  StepListNode,
} from "../types";
import { VisualView } from "../visual/Visual";
import { openArtifactPane } from "./VisualPanel";

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
  anchor,
  label,
  placeholder,
  value,
  readOnly,
}: {
  planId: string;
  anchor: AnnotationAnchor;
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
        onChange={(e) => useStore.getState().setPlanNote(planId, anchor, e.target.value)}
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

// ---- text ranges -------------------------------------------------------------

/**
 * Prose, split into the phrases you can point at.
 *
 * Only while annotating: the rest of the time this is rendered markdown, which
 * is what it is for. The offsets come from the **source string**, never from the
 * rendered DOM — `**bold**` is four characters shorter on screen than in the
 * text the agent holds, so an offset taken from the page would address the
 * wrong characters (`textSegments` in `../plan/anchors.ts`).
 */
function AnnotatableText({
  planId,
  node,
  draft,
}: {
  planId: string;
  node: MarkdownNode;
  draft: PlanDraft;
}) {
  return (
    <p className="wb-plan-phrases">
      {textSegments(node.text).map((segment) => {
        const anchor = rangeAnchor(node.node_id, segment.start, segment.end);
        const key = anchorKey(anchor);
        const noted = draft.notes[key] !== undefined;
        return (
          <button
            key={key}
            type="button"
            className={"wb-plan-phrase" + (noted ? " is-noted" : "")}
            aria-label={`Annotate “${segment.text}”`}
            onClick={() => useStore.getState().startPlanNote(planId, anchor)}
          >
            {segment.text}
            {noted && (
              <span className="wb-vis-pin" aria-hidden="true">
                ✎
              </span>
            )}
          </button>
        );
      })}
    </p>
  );
}

// ---- notes -------------------------------------------------------------------

/** One attached note: its target, its text, and the two things you can do to it. */
function NoteRow({
  planId,
  plan,
  note,
  editing,
}: {
  planId: string;
  plan: PlanArtifact;
  note: AnchoredNote;
  editing: boolean;
}) {
  const key = anchorKey(note.anchor);
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    // Picking a part should land the caret in the note it just opened; a mode
    // that makes you reach for the mouse again is half a mode. At the *end* of
    // what is there, because `focus()` alone leaves the caret at offset 0 and
    // reopening a note to add a sentence then prepends it — caught by the E2E
    // journey, which is the only place typing-after-focus is real.
    const field = box.current;
    if (!editing || field === null) return;
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
  }, [editing]);
  return (
    <li className={"wb-plan-note-row" + (editing ? " is-editing" : "")} data-anchor={key}>
      <span className="wb-plan-note-target u-truncate" title={anchorLabel(note.anchor, plan)}>
        {anchorLabel(note.anchor, plan)}
      </span>
      {editing ? (
        <textarea
          ref={box}
          rows={2}
          className="wb-plan-note-text"
          value={note.text}
          maxLength={MAX_ANNOTATION}
          placeholder="What is wrong with this part?"
          spellCheck={false}
          aria-label={`Note on ${anchorLabel(note.anchor, plan)}`}
          onChange={(e) => useStore.getState().setPlanNote(planId, note.anchor, e.target.value)}
        />
      ) : (
        <span className="wb-plan-note-text">{note.text}</span>
      )}
      <span className="wb-plan-note-actions">
        {editing ? (
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-sm"
            onClick={() => useStore.getState().stopPlanNote(planId)}
          >
            Done
          </button>
        ) : (
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-sm"
            onClick={() => useStore.getState().startPlanNote(planId, note.anchor)}
          >
            Edit
          </button>
        )}
        <button
          type="button"
          className="wb-btn wb-btn-ghost wb-btn-sm wb-btn-deny"
          aria-label={`Remove note on ${anchorLabel(note.anchor, plan)}`}
          onClick={() => useStore.getState().removePlanNote(planId, key)}
        >
          Remove
        </button>
      </span>
    </li>
  );
}

/** Every note pointing into one node (or, with `nodeId` null, at the plan). */
function NoteList({
  plan,
  draft,
  nodeId,
}: {
  plan: PlanArtifact;
  draft: PlanDraft;
  nodeId: string | null;
}) {
  const rows = Object.values(draft.notes).filter((note) =>
    nodeId === null
      ? note.anchor.kind === "plan"
      : note.anchor.kind !== "plan" &&
        note.anchor.kind !== "node" &&
        note.anchor.node_id === nodeId,
  );
  if (rows.length === 0) return null;
  return (
    <ul className="wb-plan-notes">
      {rows.map((note) => (
        <NoteRow
          key={anchorKey(note.anchor)}
          planId={plan.plan_id}
          plan={plan}
          note={note}
          editing={draft.editing === anchorKey(note.anchor)}
        />
      ))}
    </ul>
  );
}

function NodeView({
  plan,
  node,
  draft,
  readOnly,
  annotating,
}: {
  plan: PlanArtifact;
  node: PlanNode;
  draft: PlanDraft;
  readOnly: boolean;
  annotating: boolean;
}) {
  const planId = plan.plan_id;
  const anchor = nodeAnchor(node.node_id);
  const note = noteText(draft, anchor);
  const notes = <NoteList plan={plan} draft={draft} nodeId={node.node_id} />;
  switch (node.kind) {
    case "markdown":
      return (
        <div className="wb-plan-node">
          {annotating ? (
            <AnnotatableText planId={planId} node={node} draft={draft} />
          ) : (
            <Markdown text={node.text} />
          )}
          {notes}
        </div>
      );
    case "option_group":
      return (
        <div className="wb-plan-node">
          <OptionGroup planId={planId} node={node} draft={draft} readOnly={readOnly} />
          <NoteToggle planId={planId} anchor={anchor} value={note} readOnly={readOnly} />
          {notes}
        </div>
      );
    case "step_list":
      return (
        <div className="wb-plan-node">
          <StepList node={node} />
          <NoteToggle planId={planId} anchor={anchor} value={note} readOnly={readOnly} />
          {notes}
        </div>
      );
    case "visual":
      return (
        <div className="wb-plan-node">
          <div className="wb-plan-node-actions">
            <button
              type="button"
              className="wb-btn wb-btn-ghost wb-btn-sm"
              title="Open this artifact in its own pane for full-screen review"
              onClick={() => openArtifactPane(planId, node.node_id)}
            >
              Expand
            </button>
          </div>
          <VisualView
            node={node}
            annotation={{
              nodeId: node.node_id,
              notes: Object.fromEntries(
                Object.entries(draft.notes).map(([key, value]) => [key, value.text]),
              ),
              editing: draft.editing,
              onPick: annotating
                ? (path) => {
                    useStore
                      .getState()
                      .startPlanNote(planId, { kind: "part", node_id: node.node_id, path });
                  }
                : null,
            }}
          />
          <NoteToggle planId={planId} anchor={anchor} value={note} readOnly={readOnly} />
          {notes}
        </div>
      );
    case "question":
      return (
        <div className="wb-plan-node">
          <div className="wb-plan-question">{node.text}</div>
          <Annotation
            planId={planId}
            anchor={anchor}
            label="Your answer"
            placeholder="Answer the agent's question"
            value={note}
            readOnly={readOnly}
          />
          {notes}
        </div>
      );
  }
}

/** Per-node note: hidden behind one ghost button until asked for, so an
 * unannotated plan stays as quiet as the design wants it. */
function NoteToggle({
  planId,
  anchor,
  value,
  readOnly,
}: {
  planId: string;
  anchor: AnnotationAnchor;
  value: string;
  readOnly: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (readOnly || open || value !== "") {
    return (
      <Annotation
        planId={planId}
        anchor={anchor}
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
  const annotating = draft.annotating && !settled;
  const decide = (verdict: Exclude<PlanVerdict, "no_decision">): void =>
    useStore.getState().decidePlan(plan.plan_id, verdict);
  // Approving with a group unanswered would be an implied approval of a choice
  // the user never made — the agent would just guess. Revise/Reject need no
  // choices, so only Approve waits.
  const unchosen = unchosenOptionGroups(plan, draft);
  const hintId = `${titleId}-approve-hint`;

  // One Escape unwinds one layer: the open note first, then the mode. Handled
  // on the card rather than the window so it cannot swallow Escape from a
  // dialog or the QuickBar layered over it.
  const onKeyDown = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key !== "Escape" || !annotating) return;
    event.stopPropagation();
    const store = useStore.getState();
    if (draft.editing !== null) store.stopPlanNote(plan.plan_id);
    else store.setPlanAnnotating(plan.plan_id, false);
  };

  return (
    <section
      className={"wb-plan-card" + (annotating ? " is-annotating" : "")}
      aria-labelledby={titleId}
      onKeyDown={onKeyDown}
    >
      <header className="wb-plan-head">
        <span className="u-label">Plan</span>
        <h3 className="wb-plan-title" id={titleId}>
          {plan.title}
        </h3>
        {!settled && (
          <button
            type="button"
            className={"wb-btn wb-btn-ghost wb-btn-sm" + (annotating ? " is-active" : "")}
            aria-pressed={annotating}
            title="Point at a part of this plan and say what is wrong with it (Alt+A)"
            onClick={() => useStore.getState().setPlanAnnotating(plan.plan_id, !annotating)}
          >
            Annotate
          </button>
        )}
        {draft.verdict !== null && (
          <span className="wb-plan-verdict">{VERDICT_LABEL[draft.verdict]}</span>
        )}
      </header>
      {annotating && (
        <p className="wb-plan-annotate-hint" role="note">
          Pick any part — a cell, a point, a box, a line, a phrase — to note it. Tab moves
          between parts; Esc closes the note, then leaves annotate mode.
        </p>
      )}
      {plan.summary !== "" && <p className="wb-plan-summary">{plan.summary}</p>}
      {annotating && (
        <button
          type="button"
          className="wb-plan-whole"
          onClick={() => useStore.getState().startPlanNote(plan.plan_id, PLAN_ANCHOR)}
        >
          Note on the whole plan
        </button>
      )}
      <NoteList plan={plan} draft={draft} nodeId={null} />
      <div className="wb-plan-body">
        {plan.nodes.map((node) => (
          <NodeView
            key={node.node_id}
            plan={plan}
            node={node}
            draft={draft}
            readOnly={settled}
            annotating={annotating}
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

// ---- registration ------------------------------------------------------------

/**
 * What the plan card contributes to the window.
 *
 * Deliberately *not* a new `WorkbenchTool`. A plan card is not a capability —
 * it has no panel, no status item and no resource of its own; it lives inside
 * the Agent's chat. Registering a tool for it would put a second name for the
 * Agent in `tools.ts` and a second owner of the same state. So the Agent tool
 * spreads these into its own descriptor (`AgentPanel.tsx`), which is the
 * registry's own answer to "one capability, several modules".
 */
export const planCommands: readonly ToolCommand[] = [
  {
    id: "plan.annotate",
    title: "Annotate the plan",
    // `when` is re-read on every keystroke, so the chord is inert (and the row
    // hidden) unless a card is actually waiting for an answer.
    when: () => pendingPlanId() !== null,
    detail: () => "Point at a part and say what is wrong with it",
    run: () => {
      const planId = pendingPlanId();
      if (planId === null) return;
      const store = useStore.getState();
      store.setPlanAnnotating(planId, !(store.plans[planId]?.annotating ?? false));
    },
  },
];

/** `Alt+A` earns its chord: this is the mode the whole card exists to reach,
 * and `Alt` is the layer `shortcuts.md` may not bind (`docs/tools.md`). */
export const planShortcuts: Readonly<Record<string, readonly string[]>> = {
  "plan.annotate": ["Alt+A"],
};
