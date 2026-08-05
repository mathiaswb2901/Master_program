import { WebglAddon } from "@xterm/addon-webgl";
import type { Terminal as XTerm } from "@xterm/xterm";

/**
 * Put a terminal on the GPU, and survive the two ways that fails.
 *
 * xterm's default is the **DOM renderer** — one styled `<span>` per run of
 * cells, re-laid-out by the browser on every frame. It is xterm's slowest path
 * and it is what Workbench was shipping (audited: no canvas in the page, the
 * `.xterm` element carrying `xterm-dom-renderer-owner-1`). `@xterm/addon-webgl`
 * draws the same cells as textured quads instead, which is where the render
 * cost of a flood goes.
 *
 * It is not available everywhere, so neither failure may leave a dead terminal:
 *
 *  - **Attach can throw.** The context is created inside `activate`, i.e. inside
 *    `loadAddon`: no WebGL2 (a VM with no GPU, a blocklisted driver, software
 *    rasterization disabled), or the browser's per-page live-context budget
 *    already spent by other panels.
 *  - **The context can be lost at runtime** — driver reset, GPU sleep, the page
 *    backgrounded, another canvas evicting ours. xterm cannot revive the addon,
 *    so the only outcome that is not a blank panel is to drop it and let the DOM
 *    renderer take back over mid-session.
 *
 * Both paths end at the same place: a slower terminal that still works. The
 * caller gets the `kind` that actually took effect rather than the one it asked
 * for.
 */
export interface AttachedRenderer {
  /** Which renderer is actually drawing — `dom` means WebGL was refused. */
  kind: "webgl" | "dom";
  dispose(): void;
}

const NOOP: AttachedRenderer = { kind: "dom", dispose: () => {} };

/**
 * Attach the GPU renderer to an **already opened** terminal.
 *
 * `term.open()` must have run: the addon needs the element to hang its canvas
 * on and throws if it is missing — which the fallback below would then swallow,
 * quietly costing the very speedup this exists for.
 */
export function attachRenderer(
  term: Pick<XTerm, "loadAddon">,
  createAddon: () => WebglAddon = () => new WebglAddon(),
): AttachedRenderer {
  let addon: WebglAddon | null = null;
  try {
    addon = createAddon();
    term.loadAddon(addon);
  } catch (err) {
    console.warn("terminal: GPU renderer unavailable, using the DOM renderer", err);
    // xterm registers an addon before activating it, so one that threw on the
    // way up is still held by the terminal; drop it rather than leave it to be
    // disposed later by a teardown that cannot know it never activated.
    disposeQuietly(addon);
    return NOOP;
  }
  const live = addon;
  let disposed = false;
  const drop = (): void => {
    if (disposed) return;
    disposed = true;
    disposeQuietly(live);
  };
  live.onContextLoss(() => {
    console.warn("terminal: WebGL context lost, falling back to the DOM renderer");
    drop();
  });
  return { kind: "webgl", dispose: drop };
}

function disposeQuietly(addon: WebglAddon | null): void {
  if (addon === null) return;
  try {
    addon.dispose();
  } catch {
    // Disposing an addon that never activated is not a failure worth reporting.
  }
}
