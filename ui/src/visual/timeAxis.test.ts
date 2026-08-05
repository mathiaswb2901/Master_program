/**
 * The DST contract, on real Nordic clock-change dates.
 *
 * These three days are the whole reason the time axis is a typed domain object
 * rather than an array of labels: a generic axis draws all three as 24 hours and
 * labels them 00..23, which is silently wrong twice a year — and wrong in the
 * direction that matters, since the duplicated hour and the missing hour are
 * exactly the ones a dispatch decision is argued about.
 *
 * `server/tests/test_visuals.py` asserts the same dates against Python's tz
 * database. Two implementations, two tz databases, same expected labels.
 */

import { describe, expect, it } from "vitest";

import type { TimeAxis } from "../types";
import { clockAt, gridCaption, gridLabels, resolveGrid, tickStride, timeTicks } from "./timeAxis";

const NORDIC = "Europe/Stockholm";

function axis(start: string, stepMinutes = 60): TimeAxis {
  return { kind: "time", label: "", start, step_minutes: stepMinutes, timezone: NORDIC };
}

/** Local midnight on each of the three days, as an instant. */
const NORMAL = axis("2026-06-15T00:00:00+02:00");
const SPRING = axis("2026-03-29T00:00:00+01:00");
const AUTUMN = axis("2026-10-25T00:00:00+02:00");

describe("time axis — a normal day", () => {
  it("labels 24 distinct hours", () => {
    const labels = gridLabels(resolveGrid(NORMAL, 24));
    expect(labels.slice(0, 4)).toEqual(["00:00", "01:00", "02:00", "03:00"]);
    expect(labels.at(-1)).toBe("23:00");
    expect(new Set(labels).size).toBe(24);
  });

  it("says which day and how long it was", () => {
    expect(gridCaption(resolveGrid(NORMAL, 24))).toBe("15 Jun · Europe/Stockholm · 24 h");
  });
});

describe("time axis — spring forward is 23 hours", () => {
  it("has no 02:00 to draw", () => {
    const labels = gridLabels(resolveGrid(SPRING, 23));
    expect(labels.slice(0, 4)).toEqual(["00:00", "01:00", "03:00", "04:00"]);
    expect(labels).not.toContain("02:00");
    expect(labels.at(-1)).toBe("23:00");
  });

  it("covers the whole calendar day in 23 points", () => {
    const grid = resolveGrid(SPRING, 23);
    const end = grid.startMs + grid.count * grid.stepMs;
    // Midnight starting the 30th, in local time — i.e. the day really ended.
    expect(clockAt(end, NORDIC)).toBe("00:00");
    expect(gridCaption(grid)).toBe("29 Mar · Europe/Stockholm · 23 h");
  });

  it("is drawn 23/24ths as wide as a normal day, not the same width", () => {
    const short = resolveGrid(SPRING, 23);
    const normal = resolveGrid(NORMAL, 24);
    expect(short.count * short.stepMs).toBeLessThan(normal.count * normal.stepMs);
  });
});

describe("time axis — autumn back is 25 hours", () => {
  it("shows 02:00 twice, at two different instants", () => {
    const labels = gridLabels(resolveGrid(AUTUMN, 25));
    expect(labels.slice(0, 5)).toEqual(["00:00", "01:00", "02:00", "02:00", "03:00"]);
    expect(labels.filter((label) => label === "02:00")).toHaveLength(2);
    expect(labels.at(-1)).toBe("23:00");
  });

  it("does not invent a 25th distinct hour", () => {
    const labels = gridLabels(resolveGrid(AUTUMN, 25));
    expect(labels).toHaveLength(25);
    expect(new Set(labels).size).toBe(24);
    expect(gridCaption(resolveGrid(AUTUMN, 25))).toBe("25 Oct · Europe/Stockholm · 25 h");
  });

  it("keeps the fold at a quarter-hourly step too", () => {
    const labels = gridLabels(resolveGrid(axis("2026-10-25T00:00:00+02:00", 15), 100));
    expect(labels.filter((label) => label === "02:15")).toHaveLength(2);
    expect(labels.at(-1)).toBe("23:45");
  });
});

describe("time axis — ticks", () => {
  it("lands ticks on grid points, so no tick names an hour the day lacks", () => {
    const grid = resolveGrid(SPRING, 23);
    const labels = timeTicks(grid).map((tick) => tick.label);
    expect(labels).not.toContain("02:00");
    expect(labels.length).toBeLessThanOrEqual(7);
    for (const tick of timeTicks(grid)) expect(tick.offsetMs % grid.stepMs).toBe(0);
  });

  it("keeps the duplicated hour reachable on the long day", () => {
    const ticks = timeTicks(resolveGrid(AUTUMN, 25), 25);
    expect(ticks.map((tick) => tick.label).filter((label) => label === "02:00")).toHaveLength(2);
  });

  it("widens the stride rather than crowding the axis", () => {
    expect(tickStride(resolveGrid(NORMAL, 24), 7)).toBe(4);
    expect(tickStride(resolveGrid(axis("2026-06-15T00:00:00+02:00", 15), 96), 7)).toBe(16);
  });

  it("switches to dates once clock times stop distinguishing anything", () => {
    const fortnight = timeTicks(resolveGrid(axis("2026-06-01T00:00:00+02:00"), 336));
    expect(fortnight[0].label).toBe("01 Jun");
    expect(fortnight.at(-1)?.label).toMatch(/Jun$/);
  });

  it("returns nothing for an empty grid rather than dividing by zero", () => {
    expect(timeTicks(resolveGrid(NORMAL, 0))).toEqual([]);
    expect(gridCaption(resolveGrid(NORMAL, 0))).toBe("");
  });
});
