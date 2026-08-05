/**
 * The one part of `motion.ts` that is arithmetic rather than DOM.
 *
 * Continuing an interrupted dock animation means reading the scale the dock is
 * *currently* at, and the browser hands that back as a serialised matrix rather
 * than as the `scale()` that was written. The rest of the module needs a real
 * document and a real compositor and is asserted in `e2e/motion.spec.ts`; this
 * is the parsing, where a wrong answer would be a silently wrong start frame
 * instead of a visible failure.
 */

import { describe, expect, it } from "vitest";

import { scaleOf } from "./motion";

describe("scaleOf", () => {
  it("reads the scale out of a computed matrix", () => {
    expect(scaleOf("matrix(0.985, 0, 0, 0.985, 0, 0)")).toBeCloseTo(0.985, 6);
    expect(scaleOf("matrix(1, 0, 0, 1, 0, 0)")).toBe(1);
    expect(scaleOf("  matrix(0.9973, 0, 0, 0.9973, 0, 0)  ")).toBeCloseTo(0.9973, 6);
  });

  it("reads a 3d matrix too — a promoted layer serialises as matrix3d", () => {
    expect(scaleOf("matrix3d(0.99, 0, 0, 0, 0, 0.99, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)")).toBeCloseTo(
      0.99,
      6,
    );
  });

  it("answers 1 for anything it did not write", () => {
    // The resting value, and therefore the answer that cannot make the dock
    // start from somewhere it has never been.
    for (const value of ["none", "", "rotate(90deg)", "matrix(nonsense)"]) {
      expect(scaleOf(value), value).toBe(1);
    }
  });
});
