/**
 * The DST-aware time axis — the one piece of this renderer where a plausible
 * implementation is a lie.
 *
 * A `TimeAxis` payload is a *regular grid in absolute time*: `start` is an
 * instant, and point `i` is `start + i × step_minutes`. Nothing here ever adds
 * an hour to a wall clock, so the axis is linear in real time and a day that
 * really is 23 or 25 hours long is drawn at that length. The labels are then
 * computed **in the market's own zone**, which is where the clock change shows
 * up: on 2026-03-29 in Europe/Stockholm the labels step 01:00 → 03:00 and there
 * is no 02:00 to draw, and on 2026-10-25 there are two 02:00s at two different
 * instants — two different prices, two different dispatch decisions.
 *
 * The naive alternatives both fail on exactly those two days a year: labelling
 * by index (`hour ${i}`) invents an hour every autumn, and stepping a local
 * wall clock by an hour skips or repeats an instant. `server/tests/test_visuals.py`
 * asserts the same three days against Python's tz database — two independent
 * implementations agreeing on real dates is the evidence, not this comment.
 *
 * `Intl.DateTimeFormat` is the tz database; the zone name is validated
 * server-side (`models/visuals.py`) so an unknown one is a tool error the agent
 * can fix rather than a `RangeError` inside a chat card.
 */

import type { TimeAxis } from "../types";

/** A grid resolved to numbers: everything downstream works in milliseconds. */
export interface TimeGrid {
  startMs: number;
  stepMs: number;
  /** Points on the grid — the longest series in the chart. */
  count: number;
  timeZone: string;
}

export interface TimeTick {
  /** Milliseconds *since the grid start* — the chart's own x domain. */
  offsetMs: number;
  label: string;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 3_600_000;

/**
 * Fixed locale, not the user's. A market clock is read against a delivery
 * schedule, and "10:00 PM" or "22.00" depending on where the machine was set up
 * is a difference with no upside — plus it would make the tests assert on the
 * runner's locale rather than on the code.
 */
const LOCALE = "en-GB";

/** `h23` explicitly: `hour12: false` yields "24:00" for midnight under some ICU
 * builds, which reads as a 25th hour on a day that genuinely has 25 of them. */
const CLOCK: Intl.DateTimeFormatOptions = {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
};

const DATE: Intl.DateTimeFormatOptions = { day: "2-digit", month: "short" };

export function resolveGrid(axis: TimeAxis, count: number): TimeGrid {
  return {
    startMs: Date.parse(axis.start),
    stepMs: axis.step_minutes * MINUTE_MS,
    count,
    timeZone: axis.timezone,
  };
}

/** Wall-clock time at an instant, in the grid's zone. */
export function clockAt(ms: number, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, { ...CLOCK, timeZone }).format(new Date(ms));
}

/** Calendar date at an instant, in the grid's zone. */
export function dateAt(ms: number, timeZone: string): string {
  return new Intl.DateTimeFormat(LOCALE, { ...DATE, timeZone }).format(new Date(ms));
}

/** Local wall clock for every point on the grid — the labels a market calendar
 * would show, duplicated hour and missing hour included. */
export function gridLabels(grid: TimeGrid): string[] {
  return Array.from({ length: grid.count }, (_, i) =>
    clockAt(grid.startMs + i * grid.stepMs, grid.timeZone),
  );
}

/**
 * Tick strides, in minutes. Ticks land on grid *points*, never on a computed
 * wall-clock time: a stride of 6 hours over a 25-hour day gives 00, 06, 12, 18,
 * 23 local — five real delivery hours — rather than a 6th tick at an instant the
 * day does not contain.
 */
const STRIDES_MIN = [15, 30, 60, 120, 180, 240, 360, 720, 1440, 2880, 10080];

/** Points between ticks: the first stride that keeps the label count sane. */
export function tickStride(grid: TimeGrid, maxTicks: number): number {
  const stepMin = grid.stepMs / MINUTE_MS;
  for (const stride of STRIDES_MIN) {
    const points = Math.max(1, Math.round(stride / stepMin));
    if (points * stepMin >= stepMin && Math.ceil(grid.count / points) <= maxTicks) return points;
  }
  return Math.max(1, Math.ceil(grid.count / maxTicks));
}

/**
 * Axis ticks. Labels are clock times while the grid stays inside about a day
 * and a half, and dates beyond that — a fortnight of hourly prices labelled
 * 00:00 eleven times says nothing.
 */
export function timeTicks(grid: TimeGrid, maxTicks = 7): TimeTick[] {
  if (grid.count <= 0) return [];
  const stride = tickStride(grid, maxTicks);
  const spanMs = grid.count * grid.stepMs;
  const asDates = spanMs > 36 * HOUR_MS;
  const ticks: TimeTick[] = [];
  for (let i = 0; i < grid.count; i += stride) {
    const at = grid.startMs + i * grid.stepMs;
    ticks.push({
      offsetMs: i * grid.stepMs,
      label: asDates ? dateAt(at, grid.timeZone) : clockAt(at, grid.timeZone),
    });
  }
  return ticks;
}

/**
 * How the axis says which day it is drawing. One line under the chart rather
 * than a label on every tick — and it names both dates when the grid crosses
 * midnight, because "which day is this" is the first question asked of a
 * dispatch chart and the last one a bare 00:00 answers.
 */
export function gridCaption(grid: TimeGrid): string {
  if (grid.count <= 0) return "";
  const first = dateAt(grid.startMs, grid.timeZone);
  const last = dateAt(grid.startMs + (grid.count - 1) * grid.stepMs, grid.timeZone);
  const hours = (grid.count * grid.stepMs) / HOUR_MS;
  // A whole-hour count is worth stating exactly when it is not 24: that number
  // *is* the clock change, and it is the thing a reader should notice.
  const span = Number.isInteger(hours) ? ` · ${String(hours)} h` : "";
  return `${first === last ? first : `${first} – ${last}`} · ${grid.timeZone}${span}`;
}
