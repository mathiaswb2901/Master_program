/**
 * The spring derivation: the physics, and the curve it prints.
 *
 * The other half — that `tokens.css` actually carries what this produces — is
 * `e2e/perf/motion.test.ts`, which lives outside the browser program because it
 * has to read the stylesheet off disk.
 */

import { describe, expect, it } from "vitest";

import {
  SETTLE_EPSILON,
  SHAPES,
  SPRINGS,
  dampingRatio,
  displacement,
  settleMs,
  springShape,
  type SpringName,
} from "./springs";

/** Values and offsets of a `linear(…)` easing, in source order. */
function stops(linear: string): { value: number; percent: number | null }[] {
  const body = /^linear\((.*)\)$/.exec(linear.trim());
  expect(body, `not a linear() function: ${linear}`).not.toBeNull();
  return String(body?.[1])
    .split(",")
    .map((stop) => {
      const [value, percent] = stop.trim().split(/\s+/);
      return { value: Number(value), percent: percent === undefined ? null : Number.parseFloat(percent) };
    });
}

describe("the damping ratio", () => {
  it("is 1 at zero bounce — critical damping, the chrome default", () => {
    expect(dampingRatio(0)).toBe(1);
  });

  it("drops below 1 as bounce rises, and above 1 as it goes negative", () => {
    expect(dampingRatio(0.3)).toBeCloseTo(0.7, 10);
    expect(dampingRatio(-0.5)).toBeCloseTo(2, 10);
  });
});

describe("the response", () => {
  it("starts at full displacement and ends at none", () => {
    for (const zeta of [0.4, 0.7, 1, 1.6]) {
      const e = displacement(zeta);
      expect(e(0)).toBeCloseTo(1, 10);
      expect(Math.abs(e(40))).toBeLessThan(SETTLE_EPSILON);
    }
  });

  it("never crosses the target when critically damped", () => {
    const e = displacement(1);
    for (let tau = 0; tau < 12; tau += 0.01) expect(e(tau)).toBeGreaterThan(0);
  });

  it("crosses it exactly when the spec asks for bounce", () => {
    const e = displacement(dampingRatio(SHAPES.bounce));
    const overshoot = [];
    for (let tau = 0; tau < 12; tau += 0.01) if (e(tau) < 0) overshoot.push(tau);
    expect(overshoot.length).toBeGreaterThan(0);
    // …and by a *visible* amount, or it is a critically damped spring with
    // extra steps: 4 % of the travel is the reason this token exists.
    expect(Math.max(...overshoot.map((tau) => -e(tau)))).toBeGreaterThan(0.04);
  });

  it("settles in a fixed multiple of its duration, whatever the duration", () => {
    // The claim `tokens.css` rests on: for a fixed damping ratio the response
    // is one curve in normalised time, so two durations share one easing.
    expect(springShape(0).settleRatio).toBeCloseTo(1.347, 2);
    expect(settleMs("base") / settleMs("snap")).toBeCloseTo(
      SPRINGS.base.duration / SPRINGS.snap.duration,
      1,
    );
  });
});

describe("the emitted linear()", () => {
  it("runs from 0 to 1 with increasing offsets", () => {
    for (const bounce of Object.values(SHAPES)) {
      const points = stops(springShape(bounce).linear);
      expect(points[0].value).toBe(0);
      expect(points[points.length - 1].value).toBe(1);
      const offsets = points.slice(1, -1).map((point) => point.percent ?? -1);
      for (let i = 1; i < offsets.length; i++) expect(offsets[i]).toBeGreaterThan(offsets[i - 1]);
      expect(Math.min(...offsets)).toBeGreaterThan(0);
      expect(Math.max(...offsets)).toBeLessThan(100);
    }
  });

  it("tracks the real response within the simplification tolerance", () => {
    const shape = springShape(0);
    const e = displacement(shape.dampingRatio);
    const points = stops(shape.linear).map((point, i, all) => ({
      x: i === 0 ? 0 : i === all.length - 1 ? 1 : (point.percent ?? 0) / 100,
      y: point.value,
    }));
    const tauEnd = shape.settleRatio * 2 * Math.PI;
    for (let x = 0; x <= 1; x += 0.005) {
      const next = points.findIndex((point) => point.x >= x);
      const b = points[Math.max(next, 1)];
      const a = points[Math.max(next, 1) - 1];
      const t = b.x === a.x ? 0 : (x - a.x) / (b.x - a.x);
      const approximated = a.y + t * (b.y - a.y);
      expect(Math.abs(approximated - (1 - e(x * tauEnd)))).toBeLessThan(0.005);
    }
  });

  it("stays short enough to ship — the tail is not worth 128 points", () => {
    for (const bounce of Object.values(SHAPES)) {
      expect(stops(springShape(bounce).linear).length).toBeLessThan(30);
    }
  });
});

describe("the durations", () => {
  it("are ordered, and every named spring has one", () => {
    const names = Object.keys(SPRINGS) as SpringName[];
    expect(names).toEqual(["snap", "base", "bounce"]);
    expect(settleMs("snap")).toBeLessThan(settleMs("base"));
    expect(settleMs("base")).toBeLessThan(settleMs("bounce"));
  });

  it("keep the crisp springs inside a second", () => {
    // A chrome animation nobody can interrupt is a chrome animation in the way.
    for (const name of Object.keys(SPRINGS) as SpringName[]) {
      expect(settleMs(name)).toBeLessThanOrEqual(600);
    }
  });
});
