/**
 * Springs, derived rather than drawn.
 *
 * Workbench's motion is spring-based (DESIGN.md §5). A spring is not a
 * `cubic-bezier` — it is a second-order system whose shape follows from
 * physical parameters — so the easing tokens in `tokens.css` are *generated*
 * here rather than hand-picked, and this module is the derivation the next
 * person regenerates them from. No curve point is chosen by eye.
 *
 * ## The parameterisation
 *
 * Stiffness and damping are the physics, but nobody reasons in newtons per
 * metre. The two numbers a designer actually holds in their head are the ones
 * SwiftUI settled on — `Spring(duration:bounce:)` — and they map onto the
 * physics exactly, so this is a change of variables and not an approximation:
 *
 * ```
 *   mass  m  = 1                       (scale-free: only k/m and c/m matter)
 *   omega0   = 2*pi / duration         undamped natural frequency
 *   k        = omega0^2 * m            stiffness
 *   zeta     = 1 - bounce              damping ratio        (bounce >= 0)
 *   zeta     = 1 / (1 + bounce)        damping ratio        (bounce < 0)
 *   c        = 2 * zeta * omega0 * m   damping coefficient
 * ```
 *
 * `bounce = 0` is **critical damping** (zeta = 1): the fastest arrival that
 * never crosses the target. That is the whole chrome vocabulary — a
 * professional instrument does not wobble. `bounce > 0` is underdamped and
 * overshoots; §5 spends it in exactly one place.
 *
 * ## The response
 *
 * Released from a unit displacement with zero initial velocity, the remaining
 * displacement `e(t)` (so progress `p(t) = 1 - e(t)`) is the textbook solution:
 *
 * ```
 *   zeta < 1   e = exp(-zeta*w0*t) * [cos(wd*t) + (zeta*w0/wd)*sin(wd*t)]
 *   zeta = 1   e = exp(-w0*t) * (1 + w0*t)
 *   zeta > 1   e = exp(-zeta*w0*t) * [cosh(r*t) + (zeta*w0/r)*sinh(r*t)]
 * ```
 *
 * with `wd = w0*sqrt(1 - zeta^2)` and `r = w0*sqrt(zeta^2 - 1)`.
 *
 * A spring never mathematically arrives, so the **settling time** — the token's
 * CSS duration — is the first moment `|e(t)|` drops below {@link SETTLE_EPSILON}
 * and stays there. For critical damping that is `8.5/omega0`, i.e. about 1.353x
 * the `duration` parameter. Which is why a spring feels faster than its number:
 * it is past 90 % of the travel at under half its settling time.
 *
 * ## One shape, four durations
 *
 * `omega0` appears in the response only as the product `omega0*t`, so for a
 * fixed damping ratio the response is the *same curve* in normalised time
 * regardless of duration. A critically damped spring therefore has exactly one
 * shape, and duration is the only dial. That is why `tokens.css` carries two
 * easing functions and four durations rather than four easings — see
 * {@link springShapes} and {@link SPRINGS}.
 *
 * ## The output
 *
 * CSS cannot express a spring as a timing function, but it can express any
 * curve as `linear()` — a polyline of output values at input percentages. So the
 * response is sampled, simplified (Douglas-Peucker, so the flat tail costs a
 * couple of points while the steep front keeps its shape), and printed.
 * `springs.test.ts` asserts `tokens.css` holds exactly what
 * {@link springTokenBlock} prints, which is the regeneration mechanism: change
 * a spec here, run `npm run test`, paste what it says.
 */

/** A spring in the two numbers worth arguing about. */
export interface SpringSpec {
  /** Perceptual duration in **seconds** — omega0 = 2*pi/duration, not the settling time. */
  duration: number;
  /** 0 = critically damped (no overshoot). >0 overshoots; <0 is sluggish. */
  bounce: number;
}

/** The scale-free half of a spring: its damping ratio and the curve it traces. */
export interface SpringShape {
  bounce: number;
  /** zeta — 1 is critical damping. */
  dampingRatio: number;
  /** settlingTime / duration. Constant for a given damping ratio. */
  settleRatio: number;
  /** The generated `linear(…)` easing function, in normalised time. */
  linear: string;
}

/**
 * Remaining travel at which a spring is called arrived: 0.2 %.
 *
 * Sub-pixel for anything shorter than 500 px, which is every distance in this
 * app. Tighter buys nothing visible and makes every duration token longer;
 * looser leaves a visible last hop on the largest panel moves.
 */
export const SETTLE_EPSILON = 0.002;

/** Settling times round up to this, so the tokens read as design values. */
const DURATION_STEP_MS = 10;

/** Uniform samples taken before simplification. */
const SAMPLE_COUNT = 128;

/**
 * Douglas-Peucker tolerance, in progress units against a normalised time axis.
 *
 * 0.0015 of the travel is well under a pixel on a 400 px move and keeps the
 * emitted polylines at ~20 points instead of 128.
 */
const SIMPLIFY_EPSILON = 0.0015;

/** Hard stop for the settling search — a spec this slow is a mistake, not motion. */
const MAX_SETTLE_S = 5;

export function dampingRatio(bounce: number): number {
  return bounce >= 0 ? 1 - bounce : 1 / (1 + bounce);
}

/**
 * Remaining displacement `e(t)`, with `e(0) = 1` and `e'(0) = 0`, for the unit
 * natural frequency `omega0 = 1`. Normalised time is all any caller needs:
 * a real spring's response at `t` is this function at `omega0 * t`.
 *
 * Three branches because the characteristic roots are complex, repeated or
 * real; the underdamped branch is the only one that can return a negative value
 * (that is the overshoot).
 */
export function displacement(zeta: number): (tau: number) => number {
  if (Math.abs(zeta - 1) < 1e-9) {
    return (tau) => Math.exp(-tau) * (1 + tau);
  }
  if (zeta < 1) {
    const wd = Math.sqrt(1 - zeta * zeta);
    return (tau) =>
      Math.exp(-zeta * tau) * (Math.cos(wd * tau) + (zeta / wd) * Math.sin(wd * tau));
  }
  const r = Math.sqrt(zeta * zeta - 1);
  return (tau) => Math.exp(-zeta * tau) * (Math.cosh(r * tau) + (zeta / r) * Math.sinh(r * tau));
}

/**
 * Normalised time (in units of `1/omega0`) after which `|e|` never again
 * reaches {@link SETTLE_EPSILON}.
 *
 * Swept backwards from the exponential envelope's own bound rather than
 * forwards from zero: an underdamped spring dips below the threshold at every
 * zero crossing on its way out, and a forward scan would stop at the first one
 * and call a still-visibly-moving spring settled.
 */
function settleTau(zeta: number, e: (tau: number) => number): number {
  const step = 0.001;
  // exp(-zeta*tau) bounds every branch, so the envelope gives a time the
  // response is certainly quiet by; the scan only has to walk back from it.
  const bound = (Math.log(1 / SETTLE_EPSILON) + 6) / zeta;
  for (let tau = bound; tau > 0; tau -= step) {
    if (Math.abs(e(tau)) >= SETTLE_EPSILON) return tau + step;
  }
  return step;
}

interface Point {
  /** Normalised time, 0…1. */
  x: number;
  /** Progress, 0…1 (past 1 while overshooting). */
  y: number;
}

/** Douglas-Peucker: keep the points that carry the shape, drop the rest. */
function simplify(points: Point[], epsilon: number): Point[] {
  if (points.length < 3) return points;
  const first = points[0];
  const last = points[points.length - 1];
  const dx = last.x - first.x;
  const dy = last.y - first.y;
  const norm = Math.hypot(dx, dy) || 1;
  let worst = 0;
  let index = 0;
  for (let i = 1; i < points.length - 1; i++) {
    const p = points[i];
    const distance = Math.abs(dy * (p.x - first.x) - dx * (p.y - first.y)) / norm;
    if (distance > worst) {
      worst = distance;
      index = i;
    }
  }
  if (worst <= epsilon) return [first, last];
  return [
    ...simplify(points.slice(0, index + 1), epsilon).slice(0, -1),
    ...simplify(points.slice(index), epsilon),
  ];
}

/** Trim a rounded number to its shortest exact form (`0.5000` -> `0.5`). */
function trim(value: number, decimals: number): string {
  return String(Number(value.toFixed(decimals)));
}

/** Derive one scale-free spring shape from a bounce figure. */
export function springShape(bounce: number): SpringShape {
  const zeta = dampingRatio(bounce);
  const e = displacement(zeta);
  const tauEnd = Math.min(settleTau(zeta, e), MAX_SETTLE_S * 2 * Math.PI);

  const samples: Point[] = [];
  for (let i = 0; i < SAMPLE_COUNT; i++) {
    const x = i / (SAMPLE_COUNT - 1);
    samples.push({ x, y: 1 - e(x * tauEnd) });
  }
  // The endpoints are exact by definition: `linear()` must start at 0 and land
  // on 1, and the sampled tail is only within SETTLE_EPSILON of it.
  samples[0] = { x: 0, y: 0 };
  samples[samples.length - 1] = { x: 1, y: 1 };

  const kept = simplify(samples, SIMPLIFY_EPSILON);
  const stops = kept.map((point, i) => {
    if (i === 0) return "0";
    if (i === kept.length - 1) return "1";
    return `${trim(point.y, 4)} ${trim(point.x * 100, 2)}%`;
  });

  return {
    bounce,
    dampingRatio: zeta,
    // tau is in units of 1/omega0 and omega0 = 2*pi/duration, so
    // settlingTime = tau/omega0 = tau*duration/(2*pi).
    settleRatio: tauEnd / (2 * Math.PI),
    linear: `linear(${stops.join(", ")})`,
  };
}

/**
 * The two shapes the app uses, as CSS easing-function tokens.
 *
 * `crisp` is critical damping — every piece of chrome. `bounce` is the one
 * curve that overshoots, and DESIGN.md §5 spends it in exactly one place.
 */
export const SHAPES = { crisp: 0, bounce: 0.3 } as const;

export type ShapeName = keyof typeof SHAPES;

export function springShapes(): { name: ShapeName; shape: SpringShape }[] {
  return (Object.keys(SHAPES) as ShapeName[]).map((name) => ({
    name,
    shape: springShape(SHAPES[name]),
  }));
}

/**
 * The app's spring durations — one per distance the chrome actually travels,
 * and not one more. DESIGN.md §5.4 says which interaction takes which; a fourth
 * is one line here plus a row there, when something needs it.
 */
export const SPRINGS = {
  /** Chip, badge, glyph, tool row: small things, right now. */
  snap: { duration: 0.14, shape: "crisp" },
  /** The default. Overlays, menus, the dock in focus mode. */
  base: { duration: 0.22, shape: "crisp" },
  /** The only spring that overshoots. Success, and nothing else. */
  bounce: { duration: 0.4, shape: "bounce" },
} as const satisfies Record<string, { duration: number; shape: ShapeName }>;

export type SpringName = keyof typeof SPRINGS;

/** Settling time in ms — the CSS duration that must be paired with the shape. */
export function settleMs(name: SpringName): number {
  const { duration, shape } = SPRINGS[name];
  const seconds = duration * springShape(SHAPES[shape]).settleRatio;
  return Math.ceil((seconds * 1000) / DURATION_STEP_MS) * DURATION_STEP_MS;
}

/**
 * The exact block `tokens.css` must contain, generated.
 *
 * Regenerating the tokens is: change a spec above, run `npm run test`, paste
 * what the failure prints.
 */
export function springTokenBlock(): string {
  const shapes = springShapes().map(({ name, shape }) => {
    const omega0 = 2 * Math.PI; // per unit duration — the facts below are per-second
    const facts =
      `bounce ${trim(shape.bounce, 3)} -> zeta ${trim(shape.dampingRatio, 3)}, ` +
      `settles in ${trim(shape.settleRatio, 3)}x its duration ` +
      `(k = ${trim(omega0 * omega0, 1)}/d^2, c = ${trim(2 * shape.dampingRatio * omega0, 1)}/d)`;
    return `  /* ${facts} */\n  --ease-spring${name === "crisp" ? "" : `-${name}`}: ${shape.linear};`;
  });
  const durations = (Object.keys(SPRINGS) as SpringName[]).map((name) => {
    const { duration, shape } = SPRINGS[name];
    return (
      `  --spring-${name}-ms: ${String(settleMs(name))}ms; ` +
      `/* duration ${trim(duration, 3)}s, ${shape} */`
    );
  });
  return [...shapes, ...durations].join("\n");
}
