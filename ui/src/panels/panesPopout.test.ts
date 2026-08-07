/**
 * The interactive pop-out guard flag, `isPoppingOut()`.
 *
 * `Layouts.tsx` toasts on dockview's `onDidOpenPopoutWindowFail` for the
 * restore-time case (a saved popped-out pane the browser will not reopen). But
 * dockview fires that same event on the *interactive* Alt+P path when a popup is
 * blocked, and that path reports the block itself — so without a guard a blocked
 * Alt+P shows two stacked toasts. `popOutFocusedPane` raises this flag for the
 * duration of `addPopoutGroup` (the event fires synchronously inside it) so the
 * restore handler can stay quiet. These pin that the flag is up for exactly that
 * window and is lowered even if the pop-out throws.
 *
 * Only the browser-only modules are stubbed; `../tools` is emptied so the real
 * `popoutRefusal` finds no guard and allows the pop-out to proceed to the dock.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("../monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  loadMonaco: () => Promise.resolve({}),
  prefetchMonaco: () => undefined,
}));

const toasts = vi.hoisted(() => [] as { kind: string; message: string }[]);
vi.mock("../store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({ pushToast: (kind: string, message: string) => toasts.push({ kind, message }) }),
  }),
}));

const dock = vi.hoisted(() => ({ api: null as unknown }));
vi.mock("../dock", () => ({ dockApiHandle: () => dock.api }));

// No registered tools -> the real `popoutRefusal` blocks nothing, so
// `popOutFocusedPane` reaches `addPopoutGroup`.
vi.mock("../tools", () => ({ TOOLS: [] }));

const { popOutFocusedPane, isPoppingOut } = await import("./Panes");

function fakeDock(addPopoutGroup: () => Promise<boolean>) {
  const group = { panels: [{ id: "terminal#1" }] };
  return { activeGroup: group, activePanel: undefined, groups: [group], addPopoutGroup };
}

afterEach(() => {
  toasts.length = 0;
  dock.api = null;
});

describe("isPoppingOut", () => {
  it("is false at rest", () => {
    expect(isPoppingOut()).toBe(false);
  });

  it("is raised for the duration of addPopoutGroup and lowered after a blocked popup", async () => {
    let duringCall: boolean | null = null;
    dock.api = fakeDock(() => {
      // dockview fires onDidOpenPopoutWindowFail synchronously in here; this is
      // the moment the restore handler must see the flag up.
      duringCall = isPoppingOut();
      return Promise.resolve(false); // browser blocked the popup
    });

    await popOutFocusedPane();

    expect(duringCall).toBe(true);
    expect(isPoppingOut()).toBe(false);
    // The interactive path's own toast still fires — one, correctly worded.
    expect(toasts).toHaveLength(1);
    expect(toasts[0]?.message).toContain("blocked the pop-out window");
  });

  it("lowers the flag even if the pop-out throws", async () => {
    dock.api = fakeDock(() => Promise.reject(new Error("boom")));
    await expect(popOutFocusedPane()).rejects.toThrow("boom");
    expect(isPoppingOut()).toBe(false);
  });

  it("stays down when a popup opens successfully", async () => {
    dock.api = fakeDock(() => Promise.resolve(true));
    await popOutFocusedPane();
    expect(isPoppingOut()).toBe(false);
    expect(toasts).toHaveLength(0);
  });
});
