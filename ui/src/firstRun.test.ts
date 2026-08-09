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

  /**
   * The same rule as everything above, at the error layer.
   *
   * The module advertises itself as open for extension — a third greeting adds
   * an `order` and edits nothing here — so the surfaces it collects are code it
   * has never seen. Both shipped ones swallow their own errors; the next one
   * may not. What must hold regardless is the module's one promise: no surface
   * decides another's fate. Without the containment in `greet`, the first two
   * cases below reject the shared `Promise.all` behind a `void greet(...)` and
   * the greeting vanishes for everybody, unhandled and silent.
   */
  describe("contains a surface that throws", () => {
    /** Quiet, and asserted on: a contained failure is still reported. */
    let reported: ReturnType<typeof vi.spyOn>;
    beforeEach(() => {
      reported = vi.spyOn(console, "error").mockImplementation(() => {});
    });
    afterEach(() => reported.mockRestore());

    it("still greets the others when one rejects its answer", async () => {
      const log: string[] = [];
      greetFirstRun(dockA, {
        order: 10,
        wanted: () => Promise.reject(new Error("capability forgot its try/catch")),
        open: () => {
          log.push("open:welcome");
        },
      });
      greetFirstRun(dockA, surface(log, "setup", 20));
      await settle();

      expect(log.filter((entry) => entry.startsWith("open:"))).toEqual(["open:setup"]);
      expect(reported).toHaveBeenCalledTimes(1);
    });

    it("still greets the others when one throws its answer synchronously", async () => {
      // `wanted` is only *typed* as returning a promise. A surface that is not
      // an `async` function throws before there is one to attach a handler to —
      // which is why the containment is a `try` around the `await` and not a
      // `.catch()` on the result.
      const log: string[] = [];
      greetFirstRun(dockA, {
        order: 10,
        wanted: (): Promise<boolean> => {
          throw new Error("threw before returning a promise");
        },
        open: () => {
          log.push("open:welcome");
        },
      });
      greetFirstRun(dockA, surface(log, "setup", 20));
      await settle();

      expect(log.filter((entry) => entry.startsWith("open:"))).toEqual(["open:setup"]);
      expect(reported).toHaveBeenCalledTimes(1);
    });

    it("still opens the surfaces behind one that fails to open", async () => {
      // The opens are awaited in sequence *because* the order is the point, and
      // a sequence is where one throw takes the rest of the queue with it. The
      // surface that lands in front must not be a casualty of the one below it.
      const log: string[] = [];
      greetFirstRun(dockA, {
        order: 10,
        wanted: () => Promise.resolve(true),
        open: () => {
          throw new Error("panel chunk never arrived");
        },
      });
      greetFirstRun(dockA, surface(log, "setup", 20));
      await settle();

      expect(log.filter((entry) => entry.startsWith("open:"))).toEqual(["open:setup"]);
      expect(reported).toHaveBeenCalledTimes(1);
    });
  });
});
