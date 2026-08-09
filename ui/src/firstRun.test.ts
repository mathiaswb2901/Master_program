/**
 * The first-run greeting's ordering guarantees.
 *
 * These are the assertions that would have failed against the code this module
 * replaced: two surfaces deciding independently, where the first to open flips
 * the shared "is this window arranged?" answer for the second, and the tab in
 * front is whichever request came back last.
 *
 * `./api` is mocked so nothing here touches the network; `dockview` is only a
 * type here, so the dock is an opaque identity token.
 */

import type { DockviewApi } from "dockview";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const getLayouts = vi.fn<() => Promise<{ state: { current: unknown } }>>(() =>
  Promise.resolve({ state: { current: null } }),
);
vi.mock("./api", () => ({ getLayouts: () => getLayouts() }));

import { forgetFirstRun, greetFirstRun, type FirstRunSurface } from "./firstRun";

/** Two distinct dock identities; nothing here calls a dockview method. */
const dockA = { id: "a" } as unknown as DockviewApi;
const dockB = { id: "b" } as unknown as DockviewApi;

/** Let every microtask and resolved promise in the greeting settle. */
const settle = async (): Promise<void> => {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
};

/** A surface that records what happened, in one shared transcript. */
function surface(
  log: string[],
  name: string,
  order: number,
  options: { wanted?: boolean; delayTicks?: number } = {},
): FirstRunSurface {
  return {
    order,
    wanted: async () => {
      for (let i = 0; i < (options.delayTicks ?? 0); i += 1) await Promise.resolve();
      log.push(`wanted:${name}`);
      return options.wanted ?? true;
    },
    open: () => {
      log.push(`open:${name}`);
    },
  };
}

describe("the first-run greeting", () => {
  beforeEach(() => {
    getLayouts.mockClear();
    getLayouts.mockImplementation(() => Promise.resolve({ state: { current: null } }));
  });
  afterEach(() => forgetFirstRun());

  it("asks the shared arrangement question once, after every surface has answered", async () => {
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10));
    greetFirstRun(dockA, surface(log, "setup", 20, { delayTicks: 3 }));
    await settle();

    // The bug this module exists to kill: one question for the whole greeting,
    // asked only once both surfaces had said whether they want to greet.
    expect(getLayouts).toHaveBeenCalledTimes(1);
    expect(log.indexOf("open:welcome")).toBeGreaterThan(log.indexOf("wanted:setup"));
  });

  it("opens in declared order, whichever surface answers first", async () => {
    // Registered **against** the declared order on purpose: `tools.ts` decides
    // who claims first, and the greeting's order must not be a consequence of
    // that. Without the sort both halves of this come back setup-first.
    const slowSetup: string[] = [];
    greetFirstRun(dockA, surface(slowSetup, "setup", 20, { delayTicks: 5 }));
    greetFirstRun(dockA, surface(slowSetup, "welcome", 10));
    await settle();
    forgetFirstRun();

    const slowWelcome: string[] = [];
    greetFirstRun(dockA, surface(slowWelcome, "setup", 20));
    greetFirstRun(dockA, surface(slowWelcome, "welcome", 10, { delayTicks: 5 }));
    await settle();

    // Same window either way — the highest order lands last, so it is the tab
    // in front no matter which request came back first.
    expect(slowSetup.filter((entry) => entry.startsWith("open:"))).toEqual([
      "open:welcome",
      "open:setup",
    ]);
    expect(slowWelcome.filter((entry) => entry.startsWith("open:"))).toEqual([
      "open:welcome",
      "open:setup",
    ]);
  });

  it("greets nobody when the window already has a saved arrangement", async () => {
    getLayouts.mockImplementation(() => Promise.resolve({ state: { current: { grid: 1 } } }));
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10));
    greetFirstRun(dockA, surface(log, "setup", 20));
    await settle();

    expect(log.filter((entry) => entry.startsWith("open:"))).toEqual([]);
  });

  it("greets whoever wants to when the other has been dismissed", async () => {
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10, { wanted: false }));
    greetFirstRun(dockA, surface(log, "setup", 20));
    await settle();

    expect(log.filter((entry) => entry.startsWith("open:"))).toEqual(["open:setup"]);
  });

  it("costs a returning user nothing beyond their own dismissal checks", async () => {
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10, { wanted: false }));
    greetFirstRun(dockA, surface(log, "setup", 20, { wanted: false }));
    await settle();

    expect(getLayouts).not.toHaveBeenCalled();
    expect(log.filter((entry) => entry.startsWith("open:"))).toEqual([]);
  });

  it("drops a greeting whose dock went away", async () => {
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10, { delayTicks: 2 }));
    forgetFirstRun();
    await settle();

    expect(log.filter((entry) => entry.startsWith("open:"))).toEqual([]);
  });

  it("does not greet a replaced dock on the old dock's behalf", async () => {
    const log: string[] = [];
    greetFirstRun(dockA, surface(log, "welcome", 10, { delayTicks: 2 }));
    greetFirstRun(dockB, surface(log, "setup", 20));
    await settle();

    // The dock was remounted mid-flight; only the surfaces claimed against the
    // live dock may put anything on screen.
    expect(log.filter((entry) => entry.startsWith("open:"))).toEqual(["open:setup"]);
  });
});
