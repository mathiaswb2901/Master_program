/**
 * The artifact pane: identity, resolution, rendering and annotate mode.
 *
 * Node-only static markup, like `ActivityPanel.test.tsx` — the suite renders to
 * a string rather than a DOM, so the two neighbours that read `document` or pull
 * in dockview at import (`../store`, `./Panes`) are stubbed; the module under
 * test is the real one.
 *
 * The browser half — expanding a real card, the reload that proves the binding,
 * and the zero-network safety property — is `ui/e2e/visual-panel.spec.ts`, which
 * a node renderer cannot stage.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { paneId, paneInstance } from "../panes";
import type { PlanArtifact, VisualLeaf, VisualNode } from "../types";

// `./Panes` pulls in dockview's runtime and, through the registry, Monaco. The
// only thing this module needs from it is `revealPane`, which the identity test
// asserts is called with the right pane vocabulary.
const revealPane = vi.fn();
vi.mock("./Panes", () => ({ revealPane }));

// The real store reads `document` at import (node-only suite). Actions are only
// reached from click handlers, which static rendering never fires; a draft
// factory and a `getState` stub are all the module touches at load and render.
// `getState().chats` is what the pane picker (`options`) and restored-pane title
// (`titleFor`) read, so it is a mutable holder the registration tests below drive.
const storeMock = vi.hoisted(() => ({
  chats: {} as Record<string, { items: { kind: string; plan?: unknown }[] }>,
}));

vi.mock("../store", () => ({
  emptyPlanDraft: () => ({
    choices: {},
    notes: {},
    comment: "",
    verdict: null,
    annotating: false,
    editing: null,
  }),
  useStore: Object.assign(() => undefined, { getState: () => ({ chats: storeMock.chats }) }),
}));

const {
  ArtifactView,
  artifactKey,
  findArtifact,
  openArtifactPane,
  parseArtifactKey,
  visualTool,
} = await import("./VisualPanel");

type Draft = Parameters<typeof ArtifactView>[0]["draft"];

const emptyDraft: Draft = {
  choices: {},
  notes: {},
  comment: "",
  verdict: null,
  annotating: false,
  editing: null,
};

// ---- a scene with every leaf kind ------------------------------------------

const leaves: VisualLeaf[] = [
  {
    kind: "metrics",
    title: "Totals",
    items: [{ label: "Revenue", value: "18 420", unit: "EUR", role: "neutral" }],
  },
  {
    kind: "chart",
    title: "Price",
    x: { kind: "value", label: "h", unit: "", scale: "linear" },
    y: { kind: "value", label: "Price", unit: "EUR/MWh", scale: "linear" },
    y_right: null,
    series: [{ label: "SE3", style: "line", values: [1, 2, 3], x: [], axis: "left" }],
  },
  {
    kind: "table",
    title: "Hours",
    columns: [
      { label: "Hour", type: "text", unit: "" },
      { label: "Price", type: "numeric", unit: "EUR/MWh" },
    ],
    rows: [["02:00", "-3.8"]],
    highlights: [],
  },
  {
    kind: "diagram",
    title: "Pipeline",
    nodes: [
      { id: "feed", label: "Feed", role: "neutral" },
      { id: "opt", label: "Optimizer", role: "accent" },
    ],
    edges: [{ source: "feed", target: "opt", label: "" }],
  },
  {
    kind: "code_diff",
    title: "calendar.py",
    language: "python",
    before: "range(24)\n",
    after: "delivery_hours(day)\n",
  },
];

function scene(nodeId: string): VisualNode {
  return {
    kind: "visual",
    node_id: nodeId,
    title: "Day-ahead result",
    blocks: [{ layout: "single", items: leaves }],
  };
}

function plan(planId: string, nodeId: string): PlanArtifact {
  return {
    plan_id: planId,
    title: "Åsen 2 dispatch",
    summary: "",
    nodes: [scene(nodeId)],
  };
}

const html = (node: JSX.Element): string => renderToStaticMarkup(node);

// ---- pane identity: the whole of persistence -------------------------------

describe("artifact pane identity", () => {
  it("round-trips through a pane id, including a node id with : and #", () => {
    for (const nodeId of ["scene", "a:b", "node#2", "x:y#z:1"]) {
      const key = artifactKey("abc123def456", nodeId);
      // The pane id dockview persists, and back — the plan id has no colon, so
      // the first colon is the boundary and everything after it is the node id.
      const restored = parseArtifactKey(paneInstance(paneId("visual", key)));
      expect(restored).toEqual({ planId: "abc123def456", nodeId });
    }
  });

  it("a bare `visual` pane (no artifact) parses to null", () => {
    expect(parseArtifactKey(paneInstance(paneId("visual", null)))).toBeNull();
    expect(parseArtifactKey(null)).toBeNull();
    expect(parseArtifactKey("nocolon")).toBeNull();
  });

  it("openArtifactPane reveals the pane in the registry's own vocabulary", () => {
    openArtifactPane("abc123def456", "scene");
    expect(revealPane).toHaveBeenCalledWith("visual", "abc123def456:scene");
  });
});

// ---- resolution: a restored pane is vetted before it is believed -----------

describe("findArtifact", () => {
  const chats = {
    s1: { items: [{ kind: "plan" as const, plan: plan("p1", "scene") }] },
    s2: { items: [{ kind: "plan" as const, plan: plan("p2", "other") }] },
  };

  it("resolves the plan and visual node a pane is bound to", () => {
    const found = findArtifact(chats, { planId: "p1", nodeId: "scene" });
    expect(found?.plan.plan_id).toBe("p1");
    expect(found?.node.node_id).toBe("scene");
  });

  it("two panes bound to different artifacts resolve independently", () => {
    const a = findArtifact(chats, { planId: "p1", nodeId: "scene" });
    const b = findArtifact(chats, { planId: "p2", nodeId: "other" });
    expect(a?.plan.plan_id).toBe("p1");
    expect(b?.plan.plan_id).toBe("p2");
    expect(a?.node).not.toBe(b?.node);
  });

  it("returns null for an artifact the fleet no longer holds (a restart)", () => {
    expect(findArtifact(chats, { planId: "gone", nodeId: "scene" })).toBeNull();
    expect(findArtifact(chats, { planId: "p1", nodeId: "missing" })).toBeNull();
    expect(findArtifact({}, { planId: "p1", nodeId: "scene" })).toBeNull();
  });
});

// ---- rendering: the same scene graph, every leaf ---------------------------

describe("ArtifactView", () => {
  const p = plan("p1", "scene");
  const node = p.nodes[0] as VisualNode;

  it("renders every leaf kind through the shared renderer", () => {
    const markup = html(<ArtifactView plan={p} node={node} draft={emptyDraft} />);
    expect(markup).toContain("wb-vis-metrics");
    expect(markup).toContain("wb-vis-chart");
    expect(markup).toContain("wb-vis-table");
    expect(markup).toContain("wb-vis-diagram");
    expect(markup).toContain("wb-vis-diff");
    // The artifact's own frame, and the title falling back to the node's.
    expect(markup).toContain("wb-artifact");
    expect(markup).toContain("Day-ahead result");
  });

  it("offers annotate, and outside annotate mode draws no pick targets", () => {
    const markup = html(<ArtifactView plan={p} node={node} draft={emptyDraft} />);
    expect(markup).toContain(">Annotate<");
    // A part is a plain cell until the mode is on — no button, no data-anchor.
    expect(markup).not.toContain("Annotate Hours");
    expect(markup).not.toContain("data-anchor");
  });

  it("in annotate mode every anchorable part becomes a real target", () => {
    const draft: Draft = { ...emptyDraft, annotating: true };
    const markup = html(<ArtifactView plan={p} node={node} draft={draft} />);
    // A table cell is now an annotate button naming its datum, and the whole
    // drawing carries the pick affordance.
    expect(markup).toContain("data-anchor");
    expect(markup).toMatch(/aria-label="Annotate [^"]*row 1/);
    expect(markup).toContain("Note on the whole artifact");
  });

  it("shows notes in place and in the list, in both modes", () => {
    const anchor = { kind: "part" as const, node_id: "scene", path: ["leaf", 2, "row", 0] };
    const draft: Draft = {
      ...emptyDraft,
      notes: { [JSON.stringify(["part", "scene", "leaf", 2, "row", 0])]: { anchor, text: "check fold-1" } },
    };
    const markup = html(<ArtifactView plan={p} node={node} draft={draft} />);
    expect(markup).toContain("check fold-1");
    expect(markup).toContain("wb-plan-note-row");
  });

  it("a settled plan renders read-only — no Annotate, no note controls", () => {
    const anchor = { kind: "node" as const, node_id: "scene", path: [] };
    const draft: Draft = {
      ...emptyDraft,
      verdict: "approve",
      notes: { [JSON.stringify(["node", "scene"])]: { anchor, text: "looks right" } },
    };
    const markup = html(<ArtifactView plan={p} node={node} draft={draft} />);
    expect(markup).toContain("Approved");
    expect(markup).not.toContain(">Annotate<");
    expect(markup).not.toContain(">Remove<");
    // The note is still shown, just not editable.
    expect(markup).toContain("looks right");
  });
});

// ---- registration: the pane picker's rows and a restored pane's title -------

describe("visualTool.panel.instances", () => {
  const instances = visualTool.panel?.instances;

  afterEach(() => {
    storeMock.chats = {};
  });

  it("options() lists a row per live visual artifact, keyed by its full binding", () => {
    // Two plans, two scenes — the picker offers both, each opening *its* artifact.
    storeMock.chats = {
      s1: { items: [{ kind: "plan", plan: plan("plana1b2c3d4", "scene") }] },
      s2: { items: [{ kind: "plan", plan: plan("planaaaa0000", "other") }] },
    };
    const rows = instances?.options?.() ?? [];
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.key())).toEqual(["plana1b2c3d4:scene", "planaaaa0000:other"]);
    for (const row of rows) {
      expect(row).toMatchObject({
        id: `visual.${row.key()}`,
        title: "Day-ahead result",
        detail: "expand this artifact",
        category: "Artifacts",
      });
    }
  });

  it("options() says nothing when no plan is drawing an artifact", () => {
    expect(instances?.options?.()).toEqual([]);
  });

  it("titleFor() names a live artifact by its node title", () => {
    storeMock.chats = { s1: { items: [{ kind: "plan", plan: plan("plana1b2c3d4", "scene") }] } };
    expect(instances?.titleFor?.("plana1b2c3d4:scene")).toBe("Day-ahead result");
  });

  it("titleFor() falls back to the node id when the artifact is gone", () => {
    // A restored pane whose live plan the restart forgot: the node id still reads
    // as something, unlike a raw pane id.
    storeMock.chats = {};
    expect(instances?.titleFor?.("plana1b2c3d4:scene")).toBe("scene");
  });

  it("titleFor() returns 'Artifact' for a bare or malformed key", () => {
    // No colon, and empty — neither names a `planId:nodeId` binding.
    expect(instances?.titleFor?.("nocolon")).toBe("Artifact");
    expect(instances?.titleFor?.("")).toBe("Artifact");
  });
});
