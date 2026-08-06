/**
 * Browser-side instrumentation for the perf lane.
 *
 * Everything here is a *measurement*, not an assertion — the specs decide what
 * is a budget and what is merely recorded. Four sources, because they answer
 * four different complaints:
 *
 * * **PerformanceNavigationTiming + paint entries** — "it takes forever to
 *   start". Where the launch time went: network, parse, first paint.
 * * **`PerformanceObserver('event')` with `durationThreshold: 0`** — "it feels
 *   laggy when I click". The default threshold (104 ms) hides exactly the
 *   40–100 ms range that separates *instant* from *fine*, which is the range
 *   this product is being judged on. Zero, and let the report show the tail.
 * * **`longtask` and `long-animation-frame`** — *why* it felt laggy. A long
 *   task names the script that blocked; a LoAF names the frame that missed,
 *   including style/layout time no long task covers.
 * * **a rAF sampler** — the continuous case (scrolling, dragging), where no
 *   single event is slow and the thing that is wrong is the frame rhythm.
 * * **`layout-shift`** — "it jumped while I was reading it". The one thing a
 *   *faster* first paint can make worse: render before the webfont arrives and
 *   the swap can reflow the shell. Collected so that trade is measured rather
 *   than assumed (`launch.spec.ts`).
 *
 * Installed with `addInitScript`, so the observers exist before the app's first
 * line runs and `buffered: true` picks up what happened before that.
 */

import type { Page, TestInfo } from "@playwright/test";

/** Where the perf hooks live on `window`. One name, so the app never sees it. */
const GLOBAL = "__wbPerf";

export interface EventSample {
  name: string;
  startTime: number;
  duration: number;
  processingTime: number;
}

export interface TaskSample {
  startTime: number;
  duration: number;
  blockingDuration?: number;
}

export interface ShiftSample {
  startTime: number;
  value: number;
  hadRecentInput: boolean;
}

export interface Telemetry {
  /** Entry types this browser actually reported on — an empty list is a real
   * finding, not a silent zero. */
  observed: string[];
  navigation: {
    responseEnd: number;
    domInteractive: number;
    domContentLoaded: number;
    loadEventEnd: number;
    transferSize: number;
  } | null;
  paints: { name: string; startTime: number }[];
  /** `performance.now()` at the first moment a file row existed in the DOM. */
  treeReady: number | null;
  /**
   * `performance.now()` at the first moment a Monaco editor had painted lines.
   * `.monaco-editor` alone appears before the first render, so the marker is
   * `.view-lines` — the point at which the user is looking at their file.
   */
  editorReady: number | null;
  /** Timestamps a spec asked for by name (see `mark`). */
  marks: Record<string, number>;
  events: EventSample[];
  eventsTruncated: boolean;
  longTasks: TaskSample[];
  longAnimationFrames: TaskSample[];
  layoutShifts: ShiftSample[];
}

/**
 * Install the observers. Call before `page.goto`.
 *
 * The function body is serialized into the page, so it may not close over
 * anything from this module — `GLOBAL` is passed as an argument for that reason.
 */
export async function installTelemetry(page: Page): Promise<void> {
  await page.addInitScript((globalName: string) => {
    const state = {
      observed: [] as string[],
      treeReady: null as number | null,
      editorReady: null as number | null,
      marks: {} as Record<string, number>,
      events: [] as EventSample[],
      eventsTruncated: false,
      longTasks: [] as TaskSample[],
      longAnimationFrames: [] as TaskSample[],
      layoutShifts: [] as ShiftSample[],
      frames: [] as number[],
      sampling: false,
    };
    (window as unknown as Record<string, unknown>)[globalName] = state;

    /** Keep the buffers bounded: `durationThreshold: 0` is a firehose. */
    const EVENT_CAP = 3000;

    const supported: readonly string[] = PerformanceObserver.supportedEntryTypes ?? [];
    const observe = (type: string, init: PerformanceObserverInit, take: (e: never) => void) => {
      if (!supported.includes(type)) return;
      try {
        new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) take(entry as never);
        }).observe({ type, buffered: true, ...init });
        state.observed.push(type);
      } catch {
        // An entry type the browser lists but refuses to observe is a fact
        // about the browser; the report says so by leaving it out.
      }
    };

    observe("event", { durationThreshold: 0 }, (entry: PerformanceEventTiming) => {
      if (state.events.length >= EVENT_CAP) {
        state.eventsTruncated = true;
        return;
      }
      state.events.push({
        name: entry.name,
        startTime: entry.startTime,
        duration: entry.duration,
        processingTime: entry.processingEnd - entry.processingStart,
      });
    });
    observe("longtask", {}, (entry: PerformanceEntry) => {
      state.longTasks.push({ startTime: entry.startTime, duration: entry.duration });
    });
    observe("long-animation-frame", {}, (entry: PerformanceEntry & { blockingDuration?: number }) => {
      state.longAnimationFrames.push({
        startTime: entry.startTime,
        duration: entry.duration,
        blockingDuration: entry.blockingDuration ?? 0,
      });
    });
    observe(
      "layout-shift",
      {},
      (entry: PerformanceEntry & { value: number; hadRecentInput: boolean }) => {
        state.layoutShifts.push({
          startTime: entry.startTime,
          value: entry.value,
          hadRecentInput: entry.hadRecentInput,
        });
      },
    );

    // Two moments are marked in the DOM rather than polled from the test: a
    // poll from outside measures its own polling interval. "Launch" ends when a
    // file row exists; "opened" ends when Monaco has painted lines.
    const watcher = new MutationObserver(() => {
      if (state.treeReady === null && document.querySelector('[role="treeitem"]') !== null) {
        state.treeReady = performance.now();
      }
      if (state.editorReady === null && document.querySelector(".view-lines") !== null) {
        state.editorReady = performance.now();
      }
      if (state.treeReady !== null && state.editorReady !== null) watcher.disconnect();
    });
    watcher.observe(document, { childList: true, subtree: true });
  }, GLOBAL);
}

/**
 * Stamp `performance.now()` in the page under a name, immediately before the
 * test does something. Used for the one interval no browser entry describes —
 * the gap between the user's click and their file being on screen — where the
 * end is observed in the page (`editorReady`) and only the start needs saying.
 */
export async function mark(page: Page, name: string): Promise<void> {
  await page.evaluate(
    ([globalName, markName]: [string, string]) => {
      const state = (window as unknown as Record<string, { marks: Record<string, number> }>)[
        globalName
      ];
      state.marks[markName] = performance.now();
    },
    [GLOBAL, name] as [string, string],
  );
}

/** Total layout shift the user did not cause — the font-swap check. */
export function cumulativeLayoutShift(telemetry: Telemetry): number {
  return telemetry.layoutShifts
    .filter((shift) => !shift.hadRecentInput)
    .reduce((total, shift) => total + shift.value, 0);
}

/** Read everything collected so far. */
export async function readTelemetry(page: Page): Promise<Telemetry> {
  return page.evaluate((globalName: string) => {
    const state = (window as unknown as Record<string, Telemetry & { frames: number[] }>)[
      globalName
    ];
    const nav = performance.getEntriesByType("navigation")[0] as
      | PerformanceNavigationTiming
      | undefined;
    return {
      observed: state.observed,
      navigation:
        nav === undefined
          ? null
          : {
              responseEnd: nav.responseEnd,
              domInteractive: nav.domInteractive,
              domContentLoaded: nav.domContentLoadedEventEnd,
              loadEventEnd: nav.loadEventEnd,
              transferSize: nav.transferSize,
            },
      paints: performance
        .getEntriesByType("paint")
        .map((p) => ({ name: p.name, startTime: p.startTime })),
      treeReady: state.treeReady,
      editorReady: state.editorReady,
      marks: state.marks,
      events: state.events,
      eventsTruncated: state.eventsTruncated,
      longTasks: state.longTasks,
      longAnimationFrames: state.longAnimationFrames,
      layoutShifts: state.layoutShifts,
    };
  }, GLOBAL);
}

export interface FrameStats {
  frames: number;
  medianMs: number;
  p95Ms: number;
  longestMs: number;
  /** Frames that missed a 60 Hz budget, and frames long enough to *look* stuck. */
  over16ms: number;
  over50ms: number;
}

/**
 * Sample frame intervals across a continuous interaction.
 *
 * The interesting number is not the mean — a scroll that drops one 300 ms frame
 * and 59 perfect ones averages fine and feels broken. p95 and the longest frame
 * are what a user reports as "it stutters".
 */
export async function sampleFrames(
  page: Page,
  action: () => Promise<void>,
): Promise<FrameStats> {
  await page.evaluate((globalName: string) => {
    const state = (window as unknown as Record<string, { frames: number[]; sampling: boolean }>)[
      globalName
    ];
    state.frames = [];
    state.sampling = true;
    let previous = performance.now();
    const tick = (now: number): void => {
      state.frames.push(now - previous);
      previous = now;
      if (state.sampling) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, GLOBAL);

  await action();

  const intervals = await page.evaluate((globalName: string) => {
    const state = (window as unknown as Record<string, { frames: number[]; sampling: boolean }>)[
      globalName
    ];
    state.sampling = false;
    // Drop the first: it measures the gap between starting the sampler and the
    // next frame, which is not a frame the user saw.
    return state.frames.slice(1);
  }, GLOBAL);

  return summarize(intervals);
}

function summarize(intervals: number[]): FrameStats {
  if (intervals.length === 0) {
    return { frames: 0, medianMs: 0, p95Ms: 0, longestMs: 0, over16ms: 0, over50ms: 0 };
  }
  const sorted = [...intervals].sort((a, b) => a - b);
  const at = (q: number): number => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
  return {
    frames: intervals.length,
    medianMs: round(at(0.5)),
    p95Ms: round(at(0.95)),
    longestMs: round(sorted[sorted.length - 1]),
    over16ms: intervals.filter((ms) => ms > 16.7).length,
    over50ms: intervals.filter((ms) => ms > 50).length,
  };
}

export function round(ms: number): number {
  return Math.round(ms * 10) / 10;
}

/**
 * Attach a measurement to the run's report, and annotate the test with a
 * one-line summary.
 *
 * Both halves matter: the JSON is what a later PR diffs against, the annotation
 * is what someone reading the HTML report sees without opening anything. This
 * is the "report" in *report the timings, block on the counts*.
 */
export async function record(
  testInfo: TestInfo,
  name: string,
  summary: string,
  data: unknown,
): Promise<void> {
  testInfo.annotations.push({ type: "perf", description: `${name}: ${summary}` });
  await testInfo.attach(`${name}.json`, {
    body: JSON.stringify(data, null, 2),
    contentType: "application/json",
  });
}
