import type { WebglAddon } from "@xterm/addon-webgl";
import type { Terminal as XTerm } from "@xterm/xterm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { attachRenderer } from "./terminalRenderer";

/** A stand-in for the addon: the node test environment has no WebGL to lose. */
function fakeAddon(overrides: Partial<{ dispose: () => void }> = {}) {
  const listeners: Array<() => void> = [];
  const addon = {
    disposed: 0,
    onContextLoss(listener: () => void) {
      listeners.push(listener);
      return { dispose: () => {} };
    },
    dispose() {
      addon.disposed += 1;
      overrides.dispose?.();
    },
    loseContext: () => listeners.forEach((l) => l()),
  };
  return addon;
}

function fakeTerm(onLoad?: () => void) {
  const loaded: unknown[] = [];
  const term = {
    loaded,
    loadAddon(addon: unknown) {
      loaded.push(addon);
      onLoad?.();
    },
  };
  return term;
}

type Addon = ReturnType<typeof fakeAddon>;
const asAddon = (a: Addon) => a as unknown as WebglAddon;
const asTerm = (t: ReturnType<typeof fakeTerm>) => t as unknown as Pick<XTerm, "loadAddon">;

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("attachRenderer", () => {
  it("loads the GPU renderer when the driver cooperates", () => {
    const addon = fakeAddon();
    const term = fakeTerm();
    const renderer = attachRenderer(asTerm(term), () => asAddon(addon));

    expect(renderer.kind).toBe("webgl");
    expect(term.loaded).toEqual([addon]);
  });

  it("falls back to the DOM renderer when the context cannot be created", () => {
    // `loadAddon` is where the GL context is made, so this is where a machine
    // without WebGL2 throws — not at construction.
    const term = fakeTerm(() => {
      throw new Error("WebGL2 is not supported");
    });
    const addon = fakeAddon();

    const renderer = attachRenderer(asTerm(term), () => asAddon(addon));

    expect(renderer.kind).toBe("dom");
    // The half-registered addon is dropped rather than left for a later teardown.
    expect(addon.disposed).toBe(1);
    expect(() => renderer.dispose()).not.toThrow();
  });

  it("falls back when the addon itself cannot be constructed", () => {
    const renderer = attachRenderer(asTerm(fakeTerm()), () => {
      throw new Error("no such constructor");
    });
    expect(renderer.kind).toBe("dom");
  });

  it("drops the addon when the context is lost mid-session", () => {
    const addon = fakeAddon();
    const renderer = attachRenderer(asTerm(fakeTerm()), () => asAddon(addon));

    addon.loseContext();

    expect(addon.disposed).toBe(1);
    // A dead addon left loaded is a blank terminal; the DOM renderer takes over.
    renderer.dispose();
    expect(addon.disposed).toBe(1); // teardown must not double-dispose
  });

  it("disposes exactly once however often it is asked", () => {
    const addon = fakeAddon();
    const renderer = attachRenderer(asTerm(fakeTerm()), () => asAddon(addon));

    renderer.dispose();
    renderer.dispose();

    expect(addon.disposed).toBe(1);
  });

  it("survives an addon that throws on dispose", () => {
    const addon = fakeAddon({
      dispose: () => {
        throw new Error("context already gone");
      },
    });
    const renderer = attachRenderer(asTerm(fakeTerm()), () => asAddon(addon));

    expect(() => renderer.dispose()).not.toThrow();
  });
});
