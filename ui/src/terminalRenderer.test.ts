import type { WebglAddon } from "@xterm/addon-webgl";
import type { Terminal as XTerm } from "@xterm/xterm";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { attachRenderer } from "./terminalRenderer";

/** A stand-in for the addon: the node test environment has no WebGL to lose.
 *
 * The *real* context-loss path cannot be proved here — a hand-written
 * `onContextLoss` will always call back on cue, whatever xterm's addon does
 * against a driver reset. That half is asserted against a live canvas in
 * `e2e/terminal.spec.ts`, which kills the context for real; what these tests
 * pin is this module's own bookkeeping around it. */
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
/** The loader is async in production (a dynamic `import()`); mirror that here. */
const loads = (a: Addon) => () => Promise.resolve(asAddon(a));

beforeEach(() => {
  vi.spyOn(console, "warn").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("attachRenderer", () => {
  it("loads the GPU renderer when the driver cooperates", async () => {
    const addon = fakeAddon();
    const term = fakeTerm();
    const renderer = attachRenderer(asTerm(term), loads(addon));

    expect(await renderer.ready).toBe("webgl");
    expect(term.loaded).toEqual([addon]);
  });

  it("falls back to the DOM renderer when the context cannot be created", async () => {
    // `loadAddon` is where the GL context is made, so this is where a machine
    // without WebGL2 throws — not at construction.
    const term = fakeTerm(() => {
      throw new Error("WebGL2 is not supported");
    });
    const addon = fakeAddon();

    const renderer = attachRenderer(asTerm(term), loads(addon));

    expect(await renderer.ready).toBe("dom");
    // The half-registered addon is dropped rather than left for a later teardown.
    expect(addon.disposed).toBe(1);
    expect(() => renderer.dispose()).not.toThrow();
  });

  it("falls back when the addon itself cannot be constructed", async () => {
    const renderer = attachRenderer(asTerm(fakeTerm()), () => {
      throw new Error("no such constructor");
    });
    expect(await renderer.ready).toBe("dom");
  });

  it("falls back when the addon's chunk never arrives", async () => {
    // The cost of loading on demand: the network is now in this path.
    const renderer = attachRenderer(asTerm(fakeTerm()), () =>
      Promise.reject(new Error("failed to fetch dynamically imported module")),
    );
    expect(await renderer.ready).toBe("dom");
  });

  it("never attaches to a terminal disposed while the chunk was loading", async () => {
    const addon = fakeAddon();
    const term = fakeTerm();
    const renderer = attachRenderer(asTerm(term), loads(addon));

    renderer.dispose(); // the panel unmounted before the import resolved

    expect(await renderer.ready).toBe("dom");
    // A GL context hung on a dead terminal would be leaked: nothing disposes it.
    expect(term.loaded).toEqual([]);
    expect(addon.disposed).toBe(1);
  });

  it("drops the addon when the context is lost mid-session", async () => {
    const addon = fakeAddon();
    const renderer = attachRenderer(asTerm(fakeTerm()), loads(addon));
    await renderer.ready;

    addon.loseContext();

    expect(addon.disposed).toBe(1);
    // A dead addon left loaded is a blank terminal; the DOM renderer takes over.
    renderer.dispose();
    expect(addon.disposed).toBe(1); // teardown must not double-dispose
  });

  it("disposes exactly once however often it is asked", async () => {
    const addon = fakeAddon();
    const renderer = attachRenderer(asTerm(fakeTerm()), loads(addon));
    await renderer.ready;

    renderer.dispose();
    renderer.dispose();

    expect(addon.disposed).toBe(1);
  });

  it("survives an addon that throws on dispose", async () => {
    const addon = fakeAddon({
      dispose: () => {
        throw new Error("context already gone");
      },
    });
    const renderer = attachRenderer(asTerm(fakeTerm()), loads(addon));
    await renderer.ready;

    expect(() => renderer.dispose()).not.toThrow();
  });
});
