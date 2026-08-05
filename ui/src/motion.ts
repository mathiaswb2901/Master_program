/**
 * The three things motion needs from JavaScript.
 *
 * Almost everything in DESIGN.md §5 is CSS: a transition on the tint channel, a
 * keyframe entrance on the travel channel, both parameterised by tokens so
 * reduced motion is a token override rather than a code path. Three cases
 * cannot be, and they all live here so there is exactly one place where a
 * number could escape the token file:
 *
 *  1. **The theme switch**, which must run with *no* transitions at all (§5) —
 *     the browser has no way to say "change these colours without animating
 *     them", so it is bracketed by an attribute that suppresses them.
 *  2. **The dock**, whose two state changes (focus mode, a layout switch) are
 *     driven by dockview rearranging the grid, not by a class we could put a
 *     transition on. A Web Animations call on the dock element covers both.
 *  3. **Unmounting**, because React removes an element the frame it is dropped
 *     and an exit animation needs it to stay one moment longer.
 *
 * Every number here is read from `design/tokens.css` at the moment it is used.
 * That is not ceremony: it is what makes `prefers-reduced-motion` work for the
 * JS-driven half without a second implementation of the rule.
 */

import { useEffect, useRef, useState } from "react";

import { cssVar } from "./theme";

/** Parse a CSS `<time>` token (`300ms`, `0.18s`, `0s`) to milliseconds. */
export function motionMs(token: string): number {
  const value = cssVar(token).trim();
  const match = /^(-?[\d.]+)(ms|s)$/.exec(value);
  if (match === null) return 0;
  const scale = match[2] === "s" ? 1000 : 1;
  return Number(match[1]) * scale;
}

const motionNumber = (token: string, fallback: number): number => {
  const value = Number.parseFloat(cssVar(token));
  return Number.isFinite(value) ? value : fallback;
};

// ---- 1. the theme switch -----------------------------------------------------

/** Set while a theme flip is in progress; `tokens.css` kills transitions on it. */
const SWITCHING_ATTR = "data-theme-switching";

/**
 * Run `flip` with every transition in the document suppressed.
 *
 * The audit's finding: flipping `data-theme` changes the computed value of a
 * colour on nearly every element, so every element carrying a colour transition
 * starts one — the switch became hundreds of concurrent animations and a style
 * recalculation with all of them live. This PR multiplies the number of such
 * transitions, which is what turns a slow switch into an unusable one.
 *
 * The sequence is the point. Both attribute writes happen with no forced flush
 * between them, so the browser folds them into **one** style recalculation; the
 * single `getComputedStyle` read is what forces that recalculation to happen
 * here, with transitions already off, rather than later with them on. Clearing
 * the attribute afterwards changes no colour — they are already the new
 * theme's — so nothing has a value change left to transition from.
 */
export function withoutTransitions(flip: () => void): void {
  const root = document.documentElement;
  root.setAttribute(SWITCHING_ATTR, "");
  flip();
  // One synchronous recalculation, with `transition: none` in force.
  void getComputedStyle(root).backgroundColor;
  root.removeAttribute(SWITCHING_ATTR);
}

// ---- 2. the dock -------------------------------------------------------------

/**
 * What the window just did to its panels.
 *
 * `"focus"` — one panel took the whole window, or gave it back (`Alt+M`). The
 * dock is doing one thing to one surface, so it reads as a zoom: the whole dock
 * scales up from `--motion-zoom-in` and fades in on the base spring. It is one
 * composited layer for the length of one animation, which is why a panel full
 * of Monaco costs nothing to animate.
 *
 * `"shift"` — a different arrangement. Panels are recreated, moved and resized;
 * animating that geometry would be animating layout, and animating a *zoom*
 * would be claiming the panels flew somewhere they did not. So it is a short
 * dip in opacity and nothing else — long enough to say "the window changed",
 * short enough that nobody waits for it.
 */
export type DockMotion = "focus" | "shift";

/** The dock element. One per window; `App.tsx` owns the class. */
const dockElement = (): HTMLElement | null => document.querySelector<HTMLElement>(".wb-dock");

/**
 * The uniform scale factor a computed `transform` carries.
 *
 * The browser serialises any transform as a matrix, and the only transform this
 * module ever writes on the dock is a uniform `scale()`, so the first component
 * is the scale. `none` — and any shape this module did not write — reads as 1,
 * which is the resting value and therefore the safe answer.
 */
export function scaleOf(transform: string): number {
  const match = /^matrix(?:3d)?\(([^)]*)\)$/.exec(transform.trim());
  if (match === null) return 1;
  const value = Number.parseFloat(match[1].split(",")[0]);
  return Number.isFinite(value) ? value : 1;
}

/**
 * Where the dock is *at this instant*, or `null` when nothing is moving it.
 *
 * Must be read before the running animation is cancelled. A `fill: none`
 * animation contributes to computed style only while it is in its active
 * interval, so cancelling first hands back the un-animated value — opacity 1,
 * scale 1 — and the sample would report "settled" about an element half way
 * through a spring.
 */
function dockInFlight(element: HTMLElement): { opacity: string; scale: number } | null {
  if (element.getAnimations().length === 0) return null;
  const style = getComputedStyle(element);
  return { opacity: style.opacity, scale: scaleOf(style.transform) };
}

/**
 * Animate the dock through a state change it has already made.
 *
 * Deliberately *after* the fact: dockview has rearranged the grid and the new
 * layout is already correct, so this only re-plays its arrival. Nothing here
 * can leave the window in a wrong state — the animation has no fill, and if
 * the browser refuses it the window is simply the way it already was.
 *
 * **Interruptible (DESIGN.md §5.1.1).** A second `Alt+M` mid-flight resolves
 * *into* the first rather than queueing behind it, and CSS springs get that for
 * free while a keyframed one does not: replaying the fixed dip would drop the
 * dock back to opacity 0.62 / scale 0.985 and climb again, a visible backward
 * pop and exactly the "snap and restart" the doctrine rules out. So the live
 * `opacity`/`transform` are sampled first and become the new starting frame.
 * The target never changes — the dock always arrives at opacity 1, scale 1 — so
 * continuing is only ever a question of where the curve starts.
 */
export function playDockMotion(kind: DockMotion): void {
  const element = dockElement();
  if (element === null || typeof element.animate !== "function") return;
  const live = dockInFlight(element);
  // Replace rather than stack: Alt+M twice in a row must not leave two
  // animations fighting over one transform.
  for (const running of element.getAnimations()) running.cancel();

  const duration = motionMs(kind === "focus" ? "--motion-dock-ms" : "--motion-shift-ms");
  if (duration <= 0) return;
  const easing = cssVar(kind === "focus" ? "--motion-dock-ease" : "--motion-shift-ease");
  const fade = motionNumber(kind === "focus" ? "--motion-dock-fade" : "--motion-shift-fade", 1);
  const zoom = kind === "focus" ? motionNumber("--motion-zoom-in", 1) : 1;

  element.animate(
    [
      {
        opacity: live === null ? String(fade) : live.opacity,
        transform: `scale(${String(live === null ? zoom : live.scale)})`,
      },
      { opacity: "1", transform: "scale(1)" },
    ],
    { duration, easing, fill: "none" },
  );
}

// ---- 3. unmounting -----------------------------------------------------------

/**
 * Keep something mounted for its exit animation.
 *
 * Returns `[present, leaving]`: render while `present`, and add the exit class
 * while `leaving`. The delay is `--motion-exit-ms`, read at the moment it is
 * needed so the reduced-motion override applies without a second code path.
 */
export function usePresence(open: boolean): [present: boolean, leaving: boolean] {
  const [present, setPresent] = useState(open);
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    if (open) {
      setPresent(true);
      setLeaving(false);
      return;
    }
    // Nothing on screen has nothing to animate away — and scheduling a timer
    // for it would flip `present` on a component that never opened.
    if (!present) return;
    setLeaving(true);
    const timer = window.setTimeout(() => {
      setPresent(false);
      setLeaving(false);
    }, motionMs("--motion-exit-ms"));
    return () => window.clearTimeout(timer);
    // `present` is read, not tracked: re-running when it changes would restart
    // the very timer that changes it.
  }, [open, present]);

  return [present, leaving];
}

export interface Leaving<T> {
  item: T;
  leaving: boolean;
}

interface Departing<T> {
  key: string;
  index: number;
  item: T;
}

/**
 * The list version: items the store has already dropped stay on screen, marked
 * `leaving`, until their exit animation has had its `--motion-exit-ms`.
 *
 * Order is preserved by re-inserting each departing item at the index it held —
 * a toast that vanishes from the middle of a stack must not jump to the end
 * while it fades. `keyOf` must be a stable reference (a module-level function);
 * it is a dependency, and an inline arrow would re-run the diff every render.
 */
export function useLeaving<T>(items: T[], keyOf: (item: T) => string): Leaving<T>[] {
  const [departing, setDeparting] = useState<Departing<T>[]>([]);
  const previous = useRef<T[]>(items);
  const timers = useRef(new Map<string, number>());

  useEffect(() => {
    const before = previous.current;
    previous.current = items;
    const live = new Set(items.map(keyOf));

    // An item that came back before its timer fired is not leaving after all.
    for (const [key, timer] of [...timers.current]) {
      if (!live.has(key)) continue;
      window.clearTimeout(timer);
      timers.current.delete(key);
      setDeparting((current) => current.filter((entry) => entry.key !== key));
    }

    const gone = before
      .map((item, index) => ({ key: keyOf(item), index, item }))
      .filter((entry) => !live.has(entry.key) && !timers.current.has(entry.key));
    if (gone.length === 0) return;
    setDeparting((current) => [...current, ...gone]);
    const delay = motionMs("--motion-exit-ms");
    for (const entry of gone) {
      timers.current.set(
        entry.key,
        window.setTimeout(() => {
          timers.current.delete(entry.key);
          setDeparting((current) => current.filter((other) => other.key !== entry.key));
        }, delay),
      );
    }
  }, [items, keyOf]);

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const out: Leaving<T>[] = items.map((item) => ({ item, leaving: false }));
  for (const entry of [...departing].sort((a, b) => a.index - b.index)) {
    out.splice(Math.min(entry.index, out.length), 0, { item: entry.item, leaving: true });
  }
  return out;
}
