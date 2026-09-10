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
    {
      getState: () => ({
        pushToast: () => undefined,
        openFiles: [],
        openQuickPick: () => undefined,
        adoptWorkspace: () => Promise.resolve(),
      }),
    },
  ),
}));

vi.mock("./tools", () => ({ TOOLS: [] }));

// The recent-list check is the one decision in this module that is not pure —
// it reads the workspace state the tool fetched — so the api is stubbed rather
// than the state reached into. `openWorkspacePicker` is the exported door to
// that fetch (it refreshes as it opens), which is how the test seeds a list
// without a socket.
const apiMocks = vi.hoisted(() => ({
  getWorkspace: vi.fn(),
  switchWorkspace: vi.fn(),
  ApiError: class ApiError extends Error {
    detail = "";
  },
}));

vi.mock("./api", () => apiMocks);

const { looksLikeAbsolutePath, openRecentWorkspace, openWorkspacePicker, switchWarning, workspaceRows } =
  await import("./panels/Workspaces");

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
  apiMocks.getWorkspace.mockReset();
  apiMocks.switchWorkspace.mockReset();
});

/** Seed the tool's workspace state through its own refresh, the way the app
 * does. Returns once the fetch it kicked off has landed. */
async function seedWorkspace(overrides: Partial<WorkspaceState> = {}): Promise<void> {
  const seeded = state(overrides);
  apiMocks.getWorkspace.mockResolvedValue(seeded);
  apiMocks.switchWorkspace.mockResolvedValue(seeded);
  openWorkspacePicker();
  await vi.waitFor(() => {
    expect(apiMocks.getWorkspace).toHaveBeenCalled();
  });
  // …and let the `.then` that stores the result run.
  await Promise.resolve();
}

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

/**
 * `workspace.open{path}` — the CLI/agent door, and the only one that compares a
 * caller's string against the recent list.
 *
 * The list is written server-side by `str(Path(...))`, so on Windows it is
 * always backslashes. The caller's string is written by a human in a JSON file,
 * where a backslash has to be doubled and so almost nobody writes one. Those two
 * facts meeting is the bug this pins.
 */
describe("openRecentWorkspace", () => {
  it("accepts a recent written with forward slashes", async () => {
    await seedWorkspace();
    // `C:\work\beta` on the list; `C:/work/beta` in the routine. Same folder.
    expect(() => {
      openRecentWorkspace("C:/work/beta");
    }).not.toThrow();
    // Switched to the list's own canonical string, not to the caller's spelling:
    // the server is told the path it recorded.
    await vi.waitFor(() => {
      expect(apiMocks.switchWorkspace).toHaveBeenCalledWith({ path: "C:\\work\\beta" });
    });
  });

  it("still refuses a folder that is not on the list, whatever the slashes", async () => {
    await seedWorkspace();
    expect(() => {
      openRecentWorkspace("C:/work/delta");
    }).toThrow(/not on the recent workspaces list/);
    expect(apiMocks.switchWorkspace).not.toHaveBeenCalled();
  });

  it("refuses a recent whose folder has gone missing", async () => {
    await seedWorkspace();
    expect(() => {
      openRecentWorkspace("D:/archive/gamma");
    }).toThrow(/the folder is not there/);
    expect(apiMocks.switchWorkspace).not.toHaveBeenCalled();
  });

  it("treats the workspace it is already in as a no-op, not a refusal", async () => {
    await seedWorkspace();
    // The headline routine's first op: re-open the workspace this window is in,
    // written with forward slashes. Nothing to switch, and nothing to complain
    // about — a throw here is what stopped the script at op 1.
    expect(() => {
      openRecentWorkspace("c:/WORK/alpha");
    }).not.toThrow();
    expect(apiMocks.switchWorkspace).not.toHaveBeenCalled();
  });
});

describe("what the confirm dialog says", () => {
  it("describes a dirty buffer as something that cannot be saved after", () => {
    const message = switchWarning(["bid.py"], []);
    expect(message).toContain("1 file has unsaved changes: bid.py");
    expect(message).toContain("cannot be saved");
    expect(message).not.toContain("Office");
  });

  it("describes a docked document as a real window that gets closed", () => {
    // Not "unsaved changes": this app does not know whether Word has any, and
    // saying it does would be a claim it cannot make. What it *can* say is what
    // the switch will do to the window.
    const message = switchWarning([], ["report.docx"]);
    expect(message).toContain("1 document is open in Office: report.docx");
    expect(message).toContain("closes the real window");
    expect(message).not.toContain("unsaved changes");
  });

  it("says both when both are at risk, and agrees with itself about number", () => {
    const message = switchWarning(["bid.py", "notes.md"], ["a.docx", "b.xlsx"]);
    expect(message).toContain("2 files have unsaved changes: bid.py, notes.md");
    expect(message).toContain("2 documents are open in Office: a.docx, b.xlsx");
  });
});
