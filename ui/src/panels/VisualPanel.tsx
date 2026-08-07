/**
 * A visual artifact, expanded out of its inline plan card into its own dockview
 * pane for full-screen review (M5 item 3, PR 4).
 *
 * The card is where a plan is *decided*; this pane is where one of its drawn
 * artifacts is *studied*. It renders the **same** typed scene graph through the
 * **same** `../visual/Visual` components — there is no second render path, and
 * so no new way for markup, HTML, SVG-from-model or a URL to cross the wire. The
 * closed-union safety posture the card has is inherited whole: a payload string
 * is a React text node here exactly as it is in the card, and expanding an
 * artifact issues no network request (asserted in the E2E, which is the only
 * place a real browser can prove the absence).
 *
 * ## Plural, and bound by a stable pane id
 *
 * One pane per artifact. The binding is the pane's dockview id — the only thing
 * `.workbench/layouts.json` persists (`../panes.ts`) — so a saved arrangement
 * brings the pane back pointed at the *same* artifact, and two artifact panes
 * are two independent views. The instance key is `planId:nodeId`: the plan id is
 * a 12-char hex minted server-side (`models/plans.py`) and never contains a
 * colon, so the first colon reliably separates it from the node id — which is
 * agent-authored and may contain anything, colons and `#` included.
 *
 * ## Annotate mode is the card's, not a copy of it
 *
 * The draft (choices, notes, the annotate flag, which note is open) lives in the
 * store keyed by plan id, and this pane reads and writes that *same* draft. So a
 * note pointed at a chart point from the pane is the same note the card shows,
 * and it travels back to the agent in the one `PlanResponse` the card already
 * sends when the plan is decided — no second channel is opened. Toggling
 * annotate mode here toggles it on the card too, because it is one flag.
 *
 * ## A restored pane is vetted before it is believed
 *
 * Plans render live only (`ROADMAP.md`): a restart forgets them, and a pane
 * saved onto an artifact the server no longer holds cannot find it. Rather than
 * a dead pane it renders a named tombstone with the one honest recovery — close
 * it — because there is nothing on disk to reopen (DESIGN.md §6.11, product
 * principle 4c).
 */

import type { IDockviewPanelProps } from "dockview";
import { useId, useMemo } from "react";

import { anchorKey, anchorLabel, nodeAnchor } from "../plan/anchors";
import type { WorkbenchTool } from "../registry";
import {
  emptyPlanDraft,
  useStore,
  type AnchoredNote,
  type ChatItem,
  type PlanDraft,
} from "../store";
import type { PlanArtifact, PlanVerdict, VisualNode } from "../types";
import { VisualView } from "../visual/Visual";

import { paneInstance } from "../panes";
import { revealPane } from "./Panes";

import "../styles/visualPanel.css";

/** Divides the plan id from the node id in an artifact pane's instance key. */
const KEY_SEPARATOR = ":";

/** Stable identity: a fresh object per render would loop the store selector. */
const NO_DRAFT: PlanDraft = emptyPlanDraft();

const VERDICT_LABEL: Record<PlanVerdict, string> = {
  approve: "Approved",
  revise: "Revision requested",
  reject: "Rejected",
  no_decision: "No decision",
};

// ---- pane identity ----------------------------------------------------------

export interface ArtifactRef {
  planId: string;
  nodeId: string;
}

/** `(planId, nodeId)` -> the instance key half of a `visual#…` pane id. */
export function artifactKey(planId: string, nodeId: string): string {
  return `${planId}${KEY_SEPARATOR}${nodeId}`;
}

/**
 * Inverse of {@link artifactKey}. Splits on the **first** colon: the plan id
 * never contains one, so everything after it is the node id verbatim — colons
 * and `#` and all. Null for a bare `visual` pane (the default pane, which names
 * no artifact) or a key with no colon at all.
 */
export function parseArtifactKey(key: string | null): ArtifactRef | null {
  if (key === null) return null;
  const cut = key.indexOf(KEY_SEPARATOR);
  if (cut <= 0 || cut === key.length - 1) return null;
  return { planId: key.slice(0, cut), nodeId: key.slice(cut + KEY_SEPARATOR.length) };
}

// ---- finding the artifact ---------------------------------------------------

export interface ResolvedArtifact {
  plan: PlanArtifact;
  node: VisualNode;
}

/**
 * The plan and its visual node named by a pane's binding, or null if either is
 * gone. Plans live in chat items across every session (a plan id is unique
 * across sessions, so the first match is the one), so this scans them rather
 * than a second index that would be a copy to keep honest.
 */
export function findArtifact(
  chats: Record<string, { items: ChatItem[] }>,
  ref: ArtifactRef | null,
): ResolvedArtifact | null {
  if (ref === null) return null;
  for (const chat of Object.values(chats)) {
    for (const item of chat.items) {
      if (item.kind !== "plan" || item.plan.plan_id !== ref.planId) continue;
      const node = item.plan.nodes.find(
        (candidate) => candidate.kind === "visual" && candidate.node_id === ref.nodeId,
      );
      if (node !== undefined && node.kind === "visual") return { plan: item.plan, node };
      return null;
    }
  }
  return null;
}

/**
 * Open (or focus) the pane for one artifact — the card's Expand affordance.
 *
 * Goes through `revealPane` rather than `addPanel` directly: opening an artifact
 * already on screen must *focus* it, not clone it, and that rule falls out of
 * pane identity (`../panes.ts`) exactly as it does for a session or a file.
 */
export function openArtifactPane(planId: string, nodeId: string): void {
  revealPane("visual", artifactKey(planId, nodeId));
}

// ---- the notes list ---------------------------------------------------------

/** Every note attached to this artifact's node — part anchors and the
 * node-level fallback — most useful first (in draft order). */
function nodeNotes(draft: PlanDraft, nodeId: string): AnchoredNote[] {
  return Object.values(draft.notes).filter(
    (note) => note.anchor.kind !== "plan" && note.anchor.node_id === nodeId,
  );
}

/** One attached note: its target in words, its text, and edit/remove. Mirrors
 * the card's `NoteRow` so the two views read the same, scoped to this pane. */
function NoteRow({
  planId,
  plan,
  note,
  editing,
  readOnly,
}: {
  planId: string;
  plan: PlanArtifact;
  note: AnchoredNote;
  editing: boolean;
  readOnly: boolean;
}) {
  const key = anchorKey(note.anchor);
  const label = anchorLabel(note.anchor, plan);
  return (
    <li className={"wb-plan-note-row" + (editing ? " is-editing" : "")} data-anchor={key}>
      <span className="wb-plan-note-target u-truncate" title={label}>
        {label}
      </span>
      {editing && !readOnly ? (
        <textarea
          rows={2}
          className="wb-plan-note-text"
          value={note.text}
          maxLength={600}
          placeholder="What is wrong with this part?"
          spellCheck={false}
          aria-label={`Note on ${label}`}
          onChange={(e) => useStore.getState().setPlanNote(planId, note.anchor, e.target.value)}
        />
      ) : (
        <span className="wb-plan-note-text">{note.text}</span>
      )}
      {!readOnly && (
        <span className="wb-plan-note-actions">
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-sm"
            onClick={() =>
              editing
                ? useStore.getState().stopPlanNote(planId)
                : useStore.getState().startPlanNote(planId, note.anchor)
            }
          >
            {editing ? "Done" : "Edit"}
          </button>
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-sm wb-btn-deny"
            aria-label={`Remove note on ${label}`}
            onClick={() => useStore.getState().removePlanNote(planId, key)}
          >
            Remove
          </button>
        </span>
      )}
    </li>
  );
}

function NoteList({
  plan,
  node,
  draft,
  readOnly,
}: {
  plan: PlanArtifact;
  node: VisualNode;
  draft: PlanDraft;
  readOnly: boolean;
}) {
  const notes = nodeNotes(draft, node.node_id);
  if (notes.length === 0) return null;
  return (
    <ul className="wb-plan-notes">
      {notes.map((note) => (
        <NoteRow
          key={anchorKey(note.anchor)}
          planId={plan.plan_id}
          plan={plan}
          note={note}
          editing={draft.editing === anchorKey(note.anchor)}
          readOnly={readOnly}
        />
      ))}
    </ul>
  );
}

// ---- the artifact body ------------------------------------------------------

/**
 * The full-screen artifact, with annotate mode wired to the shared draft.
 *
 * Pure of dockview: it takes the resolved plan and node and the draft, so it
 * renders under `renderToStaticMarkup` for the tests and reads identically to
 * the card's `visual` case (`PlanCard.tsx`).
 */
export function ArtifactView({
  plan,
  node,
  draft,
}: {
  plan: PlanArtifact;
  node: VisualNode;
  draft: PlanDraft;
}) {
  const titleId = useId();
  const settled = draft.verdict !== null;
  const annotating = draft.annotating && !settled;
  const planId = plan.plan_id;
  const title = node.title === "" ? plan.title : node.title;

  return (
    <section
      className={"wb-artifact" + (annotating ? " is-annotating" : "")}
      aria-labelledby={titleId}
    >
      <header className="wb-artifact-head">
        <span className="u-label">Artifact</span>
        <h2 className="wb-artifact-title u-truncate" id={titleId} title={title}>
          {title}
        </h2>
        {!settled && (
          <button
            type="button"
            className={"wb-btn wb-btn-ghost wb-btn-sm" + (annotating ? " is-active" : "")}
            aria-pressed={annotating}
            title="Point at a part of this artifact and say what is wrong with it (Alt+A)"
            onClick={() => useStore.getState().setPlanAnnotating(planId, !annotating)}
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
          Pick any part — a cell, a point, a box, a line — to note it. Notes travel with the
          plan's decision. Tab moves between parts; Esc closes the note.
        </p>
      )}
      <div className="wb-artifact-body">
        <VisualView
          node={node}
          annotation={{
            nodeId: node.node_id,
            notes: Object.fromEntries(
              Object.entries(draft.notes).map(([key, value]) => [key, value.text]),
            ),
            editing: draft.editing,
            onPick: annotating
              ? (path) =>
                  useStore.getState().startPlanNote(planId, {
                    kind: "part",
                    node_id: node.node_id,
                    path,
                  })
              : null,
          }}
        />
        {annotating && (
          <button
            type="button"
            className="wb-plan-whole"
            onClick={() => useStore.getState().startPlanNote(planId, nodeAnchor(node.node_id))}
          >
            Note on the whole artifact
          </button>
        )}
        <NoteList plan={plan} node={node} draft={draft} readOnly={settled} />
      </div>
    </section>
  );
}

/** A pane bound to an artifact that is gone (a restart forgets live plans), or
 * to nothing at all (the bare `visual` default pane). Never an empty pane. */
function ArtifactTombstone({ api, ref }: { api: IDockviewPanelProps["api"]; ref: ArtifactRef | null }) {
  return (
    <div className="wb-pane-single">
      <div className="wb-pane-note">
        <span className="wb-pane-note-msg">
          {ref === null
            ? "Expand an artifact from a plan card to open it here."
            : "This artifact is no longer loaded — plans render live and a restart forgets them."}
        </span>
        <button
          type="button"
          className="wb-btn wb-btn-outline wb-btn-sm"
          onClick={() => api.close()}
        >
          Close
        </button>
      </div>
    </div>
  );
}

export function VisualPanel(props: IDockviewPanelProps) {
  const ref = useMemo(() => parseArtifactKey(paneInstance(props.api.id)), [props.api.id]);
  const chats = useStore((s) => s.chats);
  const artifact = useMemo(() => findArtifact(chats, ref), [chats, ref]);
  // NO_DRAFT is a stable module constant, and `s.plans[planId]` a stable
  // reference — so this selector never returns a fresh object, which is what
  // would loop `useSyncExternalStore` (see `Terminal.tsx`).
  const draft = useStore((s) => (ref === null ? NO_DRAFT : s.plans[ref.planId] ?? NO_DRAFT));

  if (artifact === null) return <ArtifactTombstone api={props.api} ref={ref} />;
  return (
    <div className="wb-artifact-pane">
      <ArtifactView plan={artifact.plan} node={artifact.node} draft={draft} />
    </div>
  );
}

// ---- registration -----------------------------------------------------------

/** Artifacts currently drawable, scanned from live plans — the picker's rows. */
function liveArtifacts(): { key: string; title: string }[] {
  const out: { key: string; title: string }[] = [];
  for (const chat of Object.values(useStore.getState().chats)) {
    for (const item of chat.items) {
      if (item.kind !== "plan") continue;
      for (const node of item.plan.nodes) {
        if (node.kind !== "visual") continue;
        out.push({
          key: artifactKey(item.plan.plan_id, node.node_id),
          title: node.title === "" ? item.plan.title : node.title,
        });
      }
    }
  }
  return out;
}

export const visualTool: WorkbenchTool = {
  id: "visual",
  title: "Artifact",
  panel: {
    component: VisualPanel,
    // Opened on demand from a plan card's Expand, never in the startup layout —
    // which is also what makes its tab closable.
    defaultLocation: { area: "center" },
    openByDefault: false,
    // Plural: one pane per artifact, bound by `planId:nodeId`.
    singleton: false,
    instances: {
      // A row per live artifact, so the pane picker can open one directly; the
      // registry adds the bare default-pane row itself. `key` is already known
      // (the artifact exists), so it is handed back rather than minted.
      options: () =>
        liveArtifacts().map((artifact) => ({
          id: `visual.${artifact.key}`,
          title: artifact.title,
          detail: "expand this artifact",
          category: "Artifacts",
          key: () => artifact.key,
        })),
      // Works for a restored pane whose artifact no longer resolves: the title
      // is derived from the live plan when it is there, and falls back to the
      // node id — which reads as something, unlike a raw pane id.
      titleFor: (key) => {
        const ref = parseArtifactKey(key);
        if (ref === null) return "Artifact";
        const found = findArtifact(useStore.getState().chats, ref);
        return found === null
          ? ref.nodeId
          : found.node.title === "" ? found.plan.title : found.node.title;
      },
    },
  },
};
