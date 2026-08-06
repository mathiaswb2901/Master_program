import { afterEach, describe, expect, it, vi } from "vitest";

import {
  awaitBackendReady,
  cancelShellClose,
  closeShellWindow,
  isTauri,
  onCloseRequested,
  setAttention,
  setCaptionTint,
} from "./shell";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("host detection", () => {
  it("reports a browser when Tauri did not inject its internals", () => {
    expect(isTauri()).toBe(false);
    vi.stubGlobal("window", {});
    expect(isTauri()).toBe(false);
  });

  it("reports the shell when they are present", () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
    expect(isTauri()).toBe(true);
  });
});

describe("browser mode", () => {
  // Every call has to be inert outside the shell: App.tsx and the store invoke
  // these unconditionally, and `npm run dev` has no Tauri runtime to answer.
  // Reaching the IPC here would reject (no `__TAURI_INTERNALS__`), so these
  // resolving at all is the assertion.
  it("no-ops the attention badge", async () => {
    await expect(setAttention(true)).resolves.toBeUndefined();
    await expect(setAttention(false)).resolves.toBeUndefined();
  });

  // A browser tab has no window frame to paint, so this is inert by contract —
  // App.tsx calls it on mount and on every theme flip, in both hosts.
  it("no-ops the caption tint", async () => {
    await expect(
      setCaptionTint({ caption: "#14161a", text: "#a8b0bc", border: "#3d4450" }),
    ).resolves.toBeUndefined();
  });

  it("no-ops both close paths", async () => {
    await expect(closeShellWindow()).resolves.toBeUndefined();
    await expect(cancelShellClose()).resolves.toBeUndefined();
  });

  it("returns an unlisten that never fires the handler", async () => {
    const handler = vi.fn();
    const unlisten = await onCloseRequested(handler);
    expect(unlisten).toBeTypeOf("function");
    unlisten();
    expect(handler).not.toHaveBeenCalled();
  });

  // App.tsx renders nothing until this resolves. In a browser there is no shell
  // to ask, so a wait here would be a permanently blank page.
  it("does not make the app wait for a backend it cannot ask about", async () => {
    await expect(awaitBackendReady()).resolves.toBeUndefined();
  });
});
