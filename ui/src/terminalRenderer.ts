import type { WebglAddon } from "@xterm/addon-webgl";
import type { Terminal as XTerm } from "@xterm/xterm";

/**
 * Put a terminal on the GPU, and survive every way that fails.
 *
 * xterm's default is the **DOM renderer** — one styled `<span>` per run of
 * cells, re-laid-out by the browser on every frame. It is xterm's slowest path
 * and it is what Workbench was shipping (audited: no canvas in the page, the
 * `.xterm` element carrying `xterm-dom-renderer-owner-1`). `@xterm/addon-webgl`
 * draws the same cells as textured quads instead, which is where the render
 * cost of a flood goes.
 *
 * **The addon is loaded on demand, never with the app.** It is 102 kB raw /
 * 25.9 kB gzip, and measured against the entry chunk (built both ways) that is
 * exactly what a static import adds to it — paid on first paint by every user,
 * including the ones who never open a terminal. Panels are all statically
 * imported today, so a dynamic `import()` *here* is what buys the split: the
 * addon becomes its own chunk, fetched by the first terminal to open and by
 * nothing else. The measured render win did not justify a bill charged to
 * everyone; charged only to terminal users, it costs nothing to keep.
 *
 * The load makes attaching asynchronous, so callers get `ready` rather than an
 * answer up front — and a terminal disposed while the chunk is still in flight
 * must never have an addon hung on it afterwards.
 *
 * It is not available everywhere, so no failure may leave a dead terminal:
 *
 *  - **Attach can throw.** The context is created inside `activate`, i.e. inside
 *    `loadAddon`: no WebGL2 (a VM with no GPU, a blocklisted driver, software
 *    rasterization disabled), or the browser's per-page live-context budget
 *    already spent by other panels.
 *  - **The chunk can fail to load** — offline, a cache miss against a dead
 *    server, an integrity failure.
 *  - **The context can be lost at runtime** — driver reset, GPU sleep, the page
 *    backgrounded, another canvas evicting ours. xterm cannot revive the addon,
 *    so the only outcome that is not a blank panel is to drop it and let the DOM
 *    renderer take back over mid-session. `e2e/terminal.spec.ts` forces a real
 *    context loss on the live canvas and asserts the handover.
 *
 * Every path ends at the same place: a slower terminal that still works. The
 * caller learns from `ready` which renderer actually took effect, rather than
 * the one it asked for.
 */
export type RendererKind = "webgl" | "dom";

export interface AttachedRenderer {
  /** Which renderer ended up drawing — `dom` means WebGL was never reached. */
  ready: Promise<RendererKind>;
  dispose(): void;
}

/** The default loader, and the only reference to the package outside types. */
async function importWebglAddon(): Promise<WebglAddon> {
  const { WebglAddon } = await import("@xterm/addon-webgl");
  return new WebglAddon();
}

/**
 * Attach the GPU renderer to an **already opened** terminal.
 *
 * `term.open()` must have run: the addon needs the element to hang its canvas
 * on and throws if it is missing — which the fallback below would then swallow,
 * quietly costing the very speedup this exists for.
 */
export function attachRenderer(
  term: Pick<XTerm, "loadAddon">,
  createAddon: () => Promise<WebglAddon> = importWebglAddon,
): AttachedRenderer {
  let live: WebglAddon | null = null;
  let disposed = false;

  const drop = (): void => {
    if (disposed) return;
    disposed = true;
    disposeQuietly(live);
    live = null;
  };

  const ready = (async (): Promise<RendererKind> => {
    let addon: WebglAddon | null = null;
    try {
      addon = await createAddon();
      if (disposed) {
        // The terminal was torn down while the chunk was loading. Attaching now
        // would hang a GL context on a dead terminal that nothing will dispose.
        disposeQuietly(addon);
        return "dom";
      }
      term.loadAddon(addon);
    } catch (err) {
      console.warn("terminal: GPU renderer unavailable, using the DOM renderer", err);
      // xterm registers an addon before activating it, so one that threw on the
      // way up is still held by the terminal; drop it rather than leave it to be
      // disposed later by a teardown that cannot know it never activated.
      disposeQuietly(addon);
      return "dom";
    }
    live = addon;
    addon.onContextLoss(() => {
      console.warn("terminal: WebGL context lost, falling back to the DOM renderer");
      drop();
    });
    return "webgl";
  })();

  return { ready, dispose: drop };
}

function disposeQuietly(addon: WebglAddon | null): void {
  if (addon === null) return;
  try {
    addon.dispose();
  } catch {
    // Disposing an addon that never activated is not a failure worth reporting.
  }
}
