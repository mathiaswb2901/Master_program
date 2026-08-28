/**
 * The frozen dockview-4.13 layout fixtures, put through the vetting pass that
 * stands between `layouts.json` and dockview's deserializer.
 *
 * `e2e/layoutCompat.spec.ts` proves the same three files open in a real
 * browser; this proves *why*, in milliseconds, and it is the half that fails
 * with a diff rather than with a screenshot. Both read the same frozen
 * fixtures — see that spec's header for where they came from and why they must
 * never be regenerated.
 *
 * The rule under test is `pruneLayout`'s contract: a file written by an older
 * Workbench keeps every pane this Workbench can still address, and drops
 * nothing it can. A pruner that quietly removes a pane is indistinguishable, to
 * the user, from a dockview upgrade that could not read their window.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { pruneLayout } from "../src/layouts";
import { paneVocabulary } from "../src/registry";

// The real registry means the real panel modules; Monaco's bundle and the app
// store cannot load outside a browser and neither is under test. Same stubs as
// `src/layouts.test.ts`, which imports `TOOLS` for the same reason.
vi.mock("../src/monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (p: string) => p,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  loadMonaco: () => Promise.resolve({}),
  prefetchMonaco: () => undefined,
}));

vi.mock("../src/store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({}) }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { TOOLS } = await import("../src/tools");

const FIXTURES = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "fixtures");

const fixture = (name: string): unknown =>
  JSON.parse(fs.readFileSync(path.join(FIXTURES, `${name}.json`), "utf-8"));

/** Every group id the pruned grid still has — what a `gridReferenceGroup` and
 * an `activeGroup` have to be able to name. */
function groupIds(node: unknown, into: Set<string> = new Set()): Set<string> {
  if (typeof node !== "object" || node === null) return into;
  const record = node as Record<string, unknown>;
  if (record.type === "branch" && Array.isArray(record.data)) {
    for (const child of record.data) groupIds(child, into);
    return into;
  }
  const data = record.data as Record<string, unknown> | undefined;
  if (typeof data?.id === "string") into.add(data.id);
  return into;
}

describe("a layouts.json written by dockview 4.13", () => {
  const known = paneVocabulary(TOOLS);

  it("keeps every pane of a nested grid", () => {
    const pruned = pruneLayout(fixture("layout-v4-grid"), known);
    expect(pruned).not.toBeNull();
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual([]);
    expect(Object.keys(pruned?.layout.panels ?? {}).sort()).toEqual([
      "agent",
      "editors",
      "files",
      "terminal",
      "terminal#2",
    ]);
  });

  it("carries the maximized path over an untouched grid", () => {
    const pruned = pruneLayout(fixture("layout-v4-maximized"), known);
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual([]);
    // A *path* into the tree, kept only because nothing was dropped — see
    // `pruneLayout`. Losing it means the window comes back un-maximized;
    // keeping it after a structural change means the wrong pane fills it.
    expect((pruned?.layout.grid as { maximizedNode?: unknown }).maximizedNode).toEqual({
      location: [1, 1, 1],
    });
  });

  it("keeps a popped-out pane, and the emptied group it docks back into", () => {
    // The regression this fixture exists for. When a pane pops out, dockview
    // leaves its source group behind as an invisible, view-less leaf and points
    // `popoutGroups[].gridReferenceGroup` at it — that leaf is *where the pane
    // comes home to*. Pruning it away as "an empty group" made its
    // `gridReferenceGroup` unresolvable, so the whole popout entry was dropped
    // and the pane it held vanished from the window it was saved in.
    const pruned = pruneLayout(fixture("layout-v4-popout"), known);
    expect(pruned).not.toBeNull();
    expect(pruned?.dropped).toEqual([]);
    expect(pruned?.droppedPanes).toEqual([]);

    const popouts = (pruned?.layout as unknown as { popoutGroups?: unknown[] }).popoutGroups;
    expect(popouts, "the popped-out pane survived vetting").toHaveLength(1);
    const reference = (popouts?.[0] as { gridReferenceGroup?: string }).gridReferenceGroup;
    expect(reference).toBe("8");
    expect(
      groupIds((pruned?.layout.grid as { root: unknown }).root),
      "the group the popout docks back into is still in the grid",
    ).toContain(reference);
  });
});

describe("a popped-out window that hosts a nested grid", () => {
  const known = paneVocabulary(TOOLS);

  /** dockview 7 lets a detached window hold a whole grid rather than one group,
   * and writes `grid` instead of `data` when it does. We build no UI for it —
   * but a user can drag a second pane into a popped-out window and get exactly
   * this in their file, so it is the shape a restore must not choke on. Built
   * from the frozen v4 popout fixture so the surrounding document is real. */
  function nested(): Record<string, unknown> {
    const layout = fixture("layout-v4-popout") as Record<string, unknown>;
    const popouts = layout.popoutGroups as { data: unknown; gridReferenceGroup: string }[];
    const first = popouts[0];
    return {
      ...layout,
      panels: {
        ...(layout.panels as Record<string, unknown>),
        // A pane this Workbench no longer has, dragged into the popped-out
        // window before it was removed — so the nested grid exercises pruning
        // rather than merely surviving it.
        missioncontrol: {
          id: "missioncontrol",
          contentComponent: "missioncontrol",
          title: "Mission Control",
        },
      },
      popoutGroups: [
        {
          gridReferenceGroup: first.gridReferenceGroup,
          url: "/popout.html",
          position: { top: 0, left: 0, width: 800, height: 600 },
          grid: {
            root: {
              type: "branch",
              size: 600,
              data: [
                { type: "leaf", size: 300, data: first.data },
                {
                  type: "leaf",
                  size: 300,
                  data: { id: "20", views: ["missioncontrol"], activeView: "missioncontrol" },
                },
              ],
            },
            width: 800,
            height: 600,
            orientation: "HORIZONTAL",
          },
        },
      ],
    };
  }

  it("is pruned like any other grid rather than dropped whole", () => {
    const pruned = pruneLayout(nested(), known);
    const popouts = (pruned?.layout as unknown as { popoutGroups?: Record<string, unknown>[] })
      .popoutGroups;
    expect(popouts, "the nested window survived").toHaveLength(1);
    const root = (popouts?.[0]?.grid as { root: unknown }).root;
    // `missioncontrol` is not a registered pane id, so only its leaf goes —
    // the window keeps the pane it still knows how to render.
    expect([...groupIds(root)]).toEqual(["11"]);
    expect(pruned?.dropped).toEqual(["missioncontrol"]);
  });

  it("is dropped when nothing in it can be rendered", () => {
    const layout = nested();
    const popout = (layout.popoutGroups as Record<string, unknown>[])[0];
    const grid = popout.grid as { root: { data: unknown[] } };
    grid.root.data = grid.root.data.slice(1);
    const pruned = pruneLayout(layout, known);
    expect(pruned?.layout).not.toHaveProperty("popoutGroups");
    // …and the grid is otherwise untouched: an unreadable detached window is
    // not a reason to lose the panes in the main one.
    expect(Object.keys(pruned?.layout.panels ?? {})).toContain("scratchpad");
  });
});
