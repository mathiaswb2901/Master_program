/**
 * The workspace switcher's two pure decisions: what counts as a path someone
 * typed, and what rows the picker offers.
 *
 * Both are pure on purpose. The rest of the tool is a request, a store reset
 * and a socket — proved end to end in `e2e/workspaces.spec.ts` against two real
 * seeded workspaces, which is where a switch can actually be observed to follow
 * through to the tree, the layout and shortcuts.
 *
 * The module reaches the store and the shell at import, so both are stubbed;
 * `canPickDirectory` is the one that changes what the picker renders, which is
 * why the browser-tab case is asserted rather than assumed.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkspaceState } from "./types";

const canPick = vi.fn(() => true);

vi.mock("./shell", () => ({
  canPickDirectory: () => canPick(),
  pickDirectory: () => Promise.resolve(null),
}));

vi.mock("./store", () => ({
  useStore: Object.assign(
    () => undefined,
    { getState: () => ({ pushToast: () => undefined, openFiles: [], openQuickPick: () => undefined }) },
  ),
}));

vi.mock("./tools", () => ({ TOOLS: [] }));

const { looksLikeAbsolutePath, workspaceRows } = await import("./panels/Workspaces");

function state(overrides: Partial<WorkspaceState> = {}): WorkspaceState {
  return {
    root: "C:\\work\\alpha",
    name: "alpha",
    explicit: true,
    recents: [
      { path: "C:\\work\\alpha", name: "alpha", opened_at: 3, exists: true },
      { path: "C:\\work\\beta", name: "beta", opened_at: 2, exists: true },
      { path: "D:\\archive\\gamma", name: "gamma", opened_at: 1, exists: false },
    ],
    problem: null,
    ...overrides,
  };
}

beforeEach(() => {
  canPick.mockReturnValue(true);
});

describe("looksLikeAbsolutePath", () => {
  it.each([
    "C:\\work\\thing",
    "c:/work/thing",
    "\\\\server\\share",
    "/home/me/thing",
    "~/thing",
    "~",
    "  C:\\padded  ",
  ])("accepts %j", (text) => {
    expect(looksLikeAbsolutePath(text)).toBe(true);
  });

  it.each(["", "beta", "work/thing", "..\\up", "alpha beta"])("rejects %j", (text) => {
    // A bare word is a name to filter the recent list by, not a folder to open,
    // and a *relative* path has no meaning to a server that is being told where
    // to root itself.
    expect(looksLikeAbsolutePath(text)).toBe(false);
  });
});

describe("workspaceRows", () => {
  it("offers browse plus every recent, most recent first", () => {
    const rows = workspaceRows(state(), "");
    expect(rows.map((row) => row.key)).toEqual([
      "browse",
      "C:\\work\\alpha",
      "C:\\work\\beta",
      "D:\\archive\\gamma",
    ]);
  });

  it("offers the typed path first, and only when it is one", () => {
    expect(workspaceRows(state(), "D:\\other").map((row) => row.key)[0]).toBe("open:D:\\other");
    expect(workspaceRows(state(), "beta").map((row) => row.key)[0]).toBe("browse");
  });

  it("shows the current workspace and a missing folder, both unchoosable", () => {
    // Shown, not hidden: a history that silently forgets where you were is
    // worse than one that says the drive is not plugged in.
    const rows = workspaceRows(state(), "");
    const current = rows.find((row) => row.key === "C:\\work\\alpha");
    const missing = rows.find((row) => row.key === "D:\\archive\\gamma");
    expect(current?.disabled).toBe(true);
    expect(current?.detail).toContain("current");
    expect(missing?.disabled).toBe(true);
    expect(missing?.detail).toContain("missing");
    expect(rows.find((row) => row.key === "C:\\work\\beta")?.disabled).toBeUndefined();
  });

  it("says why there is no folder dialog in a browser tab", () => {
    canPick.mockReturnValue(false);
    const browse = workspaceRows(state(), "")[0];
    expect(browse?.title).toContain("Type or paste");
    expect(browse?.detail).toContain("desktop shell");
    // Never disabled: the row still does something — it opens the prompt that
    // *is* the browser's answer — so disabling it would be a dead button.
    expect(browse?.disabled).toBeUndefined();
  });

  it("renders nothing but the openers before the first read lands", () => {
    expect(workspaceRows(null, "").map((row) => row.key)).toEqual(["browse"]);
  });
});
