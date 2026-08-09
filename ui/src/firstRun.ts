/**
 * First run, decided once — so every stranger meets the same window.
 *
 * Two capabilities greet a window nobody has arranged: the welcome card
 * (`panels/Keyboard.tsx`) teaches *the window*, and the Setup walkthrough
 * (`setup.tsx`) teaches *the connections*. Each used to decide on its own,
 * asynchronously, inside its own `onDockReady`. Two independent async chains
 * that each end in `setActive()` means the tab the window lands on is whichever
 * chain happened to finish last — and that is not a theoretical worry. Four
 * identical launches of the same fresh workspace, on one machine, produced
 * three different windows: the welcome card in front once, Setup in front once,
 * and **twice the welcome card did not open at all**.
 *
 * That third outcome is what makes this a bug rather than an untidiness. Both
 * surfaces gate themselves on the same question — *does this window already
 * have a saved arrangement?* — because a restore would remove a panel they had
 * just added. But opening a panel **is** a layout change, and the layout system
 * debounces those into `.workbench/layouts.json`. So the surface that opens
 * first turns the shared answer from "no" into "yes", and the other one, asking
 * a beat later, silently declines to greet anybody. Each surface's own comment
 * claims this rule "cannot collide"; it cannot collide with the layout system's
 * *restore*, which is what those comments were about — it collides with the
 * other greeting.
 *
 * So the question is asked **once, for all of them, before any of them opens**,
 * and the ones that want to greet are opened in a declared order rather than in
 * the order their requests came back. The surface with the highest `order`
 * opens last, which — dockview brings each opened panel forward — is the tab a
 * stranger actually lands on.
 *
 * This file names no capability, which is what lets a third greeting be added
 * without editing it: a surface declares its own `order` next to the rest of its
 * descriptor, exactly as it declares `defaultLocation`.
 */

import type { DockviewApi } from "dockview";

import * as api from "./api";

export interface FirstRunSurface {
  /**
   * Where this surface sits in the greeting. Everything wanted is opened;
   * the **highest** `order` opens last and is therefore the tab in front.
   */
  order: number;
  /**
   * Does this surface want to greet? Asked for every surface *before* any of
   * them opens, so no surface's answer can depend on another's side effects.
   * This is also where a surface records what it learned (its dismissal flag),
   * because that is true whether or not it ends up opening.
   */
  wanted: () => Promise<boolean>;
  /** Put it on screen. Only ever called when nothing will restore over it. */
  open: () => Promise<void> | void;
}

/** The dock the batch being collected belongs to. Identity, not truthiness: a
 * remounted dock is a different api object and a different greeting. */
let dock: DockviewApi | null = null;
let batch: FirstRunSurface[] = [];
let scheduled = false;

/** The dock went away (`onDockReady(null)`): drop a greeting that has not run.
 * Opening panels into a dock that is being torn down is how a "first run" ends
 * up half on screen. */
export function forgetFirstRun(): void {
  dock = null;
  batch = [];
}

/**
 * Register a first-run surface for the dock that has just become ready.
 *
 * Called synchronously from a tool's `onDockReady`. Every tool's `onDockReady`
 * for one dock runs in a single synchronous pass (`registry.notifyDockReady`),
 * so a microtask is exactly late enough to hold the whole set and early enough
 * to be long before the user can touch anything.
 */
export function greetFirstRun(api_: DockviewApi, surface: FirstRunSurface): void {
  if (dock !== api_) {
    dock = api_;
    batch = [];
  }
  batch.push(surface);
  if (scheduled) return;
  scheduled = true;
  queueMicrotask(() => {
    scheduled = false;
    void greet(dock, batch);
  });
}

async function greet(
  mine: DockviewApi | null,
  surfaces: readonly FirstRunSurface[],
): Promise<void> {
  if (mine === null || surfaces.length === 0) return;
  const answers = await Promise.all(surfaces.map((surface) => surface.wanted()));
  const wanted = surfaces.filter((_, index) => answers[index]);
  // Nobody is greeting, so the arrangement is nobody's business: a returning
  // user still pays for their own dismissal check and not one byte more, which
  // is the property each surface used to hold on its own.
  if (wanted.length === 0 || dock !== mine) return;
  try {
    // The one shared question. If a saved arrangement exists it is the truth
    // about which panels are open and a greeting would be removed by the
    // restore; if it does not, nothing will restore over what we open.
    if ((await api.getLayouts()).state.current !== null) return;
  } catch {
    // Layouts unavailable: nothing will restore over us either, so greet.
  }
  if (dock !== mine) return;
  // Sorted, and awaited one at a time. The order these land in is the whole
  // point of this module, so it may not be left to whichever `open()` resolves
  // first — one of them loads a chunk and the other does not.
  for (const surface of [...wanted].sort((a, b) => a.order - b.order)) {
    await surface.open();
  }
}
