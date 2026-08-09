/**
 * The settings write path with more than one change in flight.
 *
 * A settings panel is a surface people click *fast* — theme, then voice, inside
 * one round trip — and a whole-document PUT per click is two ways to lose an
 * answer:
 *
 *  - **the response race.** Two PUTs are outstanding; the first one's reply
 *    arrives last and its (now stale) `SettingsState` is written over the store,
 *    silently putting the user's later choice back. Nothing on screen says so.
 *  - **the stale merge base.** Each `save` merges its patch onto the *last
 *    settled* answer, so the second click builds its document from a `stored`
 *    that does not yet contain the first click — and the body it PUTs actively
 *    un-does it. That one reaches the file, so it survives a relaunch.
 *
 * Both are reproduced here the way a user makes them — two `save()` calls, no
 * `await` between them — against a fake server that answers *out of order*,
 * which is the only part a browser will not do on demand. The third test is the
 * same bug wearing a GET: a read already in flight when a write is issued must
 * not land on top of it.
 *
 * Serializing the writes closes those, and opens a second family: with one PUT
 * outstanding at a time, the question stops being "which reply wins" and
 * becomes **what happens to the document queued behind the one in flight**.
 * Three ways to lose it, one test each, all of them a click the user made and
 * was never told about:
 *
 *  - **a refusal that refuses more than its own body.** A failed PUT that
 *    clears the queue drops the newer document with it — never sent, never
 *    retried, and the caller's `save()` resolved on the shared drain as if it
 *    had worked.
 *  - **a reply rendered after it was superseded.** Applying the answer to a
 *    body that a queued document already replaced repaints the panel, and the
 *    window's palette, through a value from a round trip ago.
 *  - **a teardown that cancels instead of silencing.** The dock going away must
 *    stop the answers from landing, not stop the writes from happening: the
 *    settings document outlives the panel.
 *
 * The panel body's rendering is `panels/Settings.test.tsx`; the journey through
 * a real server is `e2e/settings.spec.ts`. This file is only about ordering.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { SettingsState, WorkbenchSettings } from "./types";

const DEFAULTS: WorkbenchSettings = { theme: "system", office_native: "auto", voice_input: false };

/**
 * A settings server small enough to script: it holds one document, records
 * every body it is given, and answers each request after a delay this file
 * chooses. Hoisted because `vi.mock` factories run before the imports.
 */
const server = vi.hoisted(() => {
  const wait = (ms: number): Promise<void> =>
    new Promise((done) => {
      setTimeout(done, ms);
    });
  const fresh = (): WorkbenchSettings => ({
    theme: "system",
    office_native: "auto",
    voice_input: false,
  });
  const fake = {
    /** The document on "disk": the last body written. */
    stored: fresh(),
    /** Every PUT body, in the order the server received them. */
    writes: [] as WorkbenchSettings[],
    /** Milliseconds each PUT takes to answer, by call index; the last repeats. */
    putDelay: [0],
    /** The same for GET. */
    getDelay: [0],
    puts: 0,
    gets: 0,
    delay(schedule: number[], turn: number): number {
      return schedule[Math.min(turn, schedule.length - 1)];
    },
    answer(stored: WorkbenchSettings): SettingsState {
      return {
        stored,
        effective: stored,
        overrides: [],
        pending_restart: [],
        path: "C:\\app-data\\Workbench\\settings.json",
        telemetry: { enabled: false, detail: "Off, and there is nothing to turn on." },
        problem: null,
      };
    },
    async get(): Promise<SettingsState> {
      // Read when the request arrives, not when it is answered — a late reply
      // carries the document as it was, which is the whole hazard.
      const snapshot = { ...fake.stored };
      await wait(fake.delay(fake.getDelay, fake.gets++));
      return fake.answer(snapshot);
    },
    async put(body: WorkbenchSettings): Promise<SettingsState> {
      const turn = fake.puts++;
      fake.writes.push({ ...body });
      fake.stored = { ...body };
      const reply = fake.answer({ ...body });
      await wait(fake.delay(fake.putDelay, turn));
      return reply;
    },
    reset(): void {
      fake.stored = fresh();
      fake.writes = [];
      fake.putDelay = [0];
      fake.getDelay = [0];
      fake.puts = 0;
      fake.gets = 0;
    },
  };
  return fake;
});

/** The window's palette, so a theme actually applied is observable — and every
 * palette it was ever put into, so a repaint *through* a superseded value is
 * observable too, which the end state alone cannot show. */
const window_ = vi.hoisted(() => ({ theme: "dark", worn: [] as string[] }));

vi.mock("./api", () => ({
  getSettings: () => server.get(),
  putSettings: (body: WorkbenchSettings) => server.put(body),
}));

vi.mock("./dock", () => ({ openPanel: () => undefined }));

vi.mock("./theme", () => ({ documentTheme: () => window_.theme }));

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({
      setTheme: (theme: string) => {
        window_.theme = theme;
        window_.worn.push(theme);
      },
    }),
    subscribe: () => () => undefined,
  }),
}));

const { refresh, save, settingsTool, useSettings } = await import("./settings");

/** What the panel's controls are showing. */
const shown = (): WorkbenchSettings | undefined => useSettings.getState().state?.stored;

/** The last body the server was told to store — the value that survives a relaunch. */
const onDisk = (): WorkbenchSettings | undefined => server.writes.at(-1);

beforeEach(async () => {
  server.reset();
  window_.theme = "dark";
  useSettings.setState({ state: null, error: null });
  await refresh();
  expect(shown()).toEqual(DEFAULTS);
  window_.worn = []; // the arrange is not the act
});

describe("two changes inside one round trip", () => {
  it("keeps the later choice when the earlier save answers last", async () => {
    // The reorder: the first PUT is slow, so its reply lands after the second's.
    server.putDelay = [30, 0];

    const first = save({ theme: "light" });
    const second = save({ voice_input: true });
    await Promise.all([first, second]);

    // Both clicks happened, so both are the answer. Before the fix the first
    // PUT's stale reply landed last and put `voice_input` back to false.
    expect(shown()).toEqual({ theme: "light", office_native: "auto", voice_input: true });
  });

  it("never PUTs a body that un-does a change the user just made", async () => {
    server.putDelay = [30, 0];

    await Promise.all([save({ theme: "light" }), save({ voice_input: true })]);

    // The store agreeing is not enough: the *document* has to carry both, or
    // the theme comes back as "system" on the next launch. Before the fix the
    // second body was merged onto a `stored` that predated the first click.
    expect(onDisk()).toEqual({ theme: "light", office_native: "auto", voice_input: true });
    expect(server.stored).toEqual({ theme: "light", office_native: "auto", voice_input: true });
  });

  it("applies the window theme from the answer that won, not the one that lost", async () => {
    server.putDelay = [30, 0];

    await Promise.all([save({ theme: "light" }), save({ theme: "dark" })]);

    expect(shown()?.theme).toBe("dark");
    expect(window_.theme).toBe("dark");
  });

  it("coalesces a burst into far fewer writes than clicks", async () => {
    server.putDelay = [20];

    await Promise.all([
      save({ theme: "light" }),
      save({ theme: "dark" }),
      save({ theme: "light" }),
      save({ voice_input: true }),
    ]);

    // Four clicks, and what matters is that the last one wins and the file is
    // written whole. One PUT per click is a write amplification a settings
    // panel does not need; the ceiling is stated rather than left open.
    expect(server.writes.length).toBeLessThanOrEqual(2);
    expect(shown()).toEqual({ theme: "light", office_native: "auto", voice_input: true });
  });
});

describe("a read already in flight", () => {
  it("cannot land on top of a write issued after it", async () => {
    // `openSettings` and the panel's own mount both re-read; a click landing
    // between the request and its answer is an ordinary Tuesday.
    server.getDelay = [40];
    const reading = refresh();
    const writing = save({ theme: "light" });
    await Promise.all([reading, writing]);

    // The GET's answer is older than the write it would overwrite. Before the
    // fix it reverted the panel *and* repainted the window back to dark.
    expect(shown()?.theme).toBe("light");
    expect(window_.theme).toBe("light");
  });
});

describe("a write that fails", () => {
  it("reports it and leaves the last good state in place", async () => {
    const boom = vi.spyOn(server, "put").mockRejectedValueOnce(new Error("access is denied"));
    await save({ theme: "light" });
    expect(useSettings.getState().error).toBe("access is denied");
    expect(shown()).toEqual(DEFAULTS);
    boom.mockRestore();
  });

  it("does not swallow the change queued behind it", async () => {
    // The user clicks theme, then voice, inside one round trip; the first PUT
    // fails. Only one PUT is ever outstanding, so the voice click is sitting in
    // the queue when the refusal arrives — and clearing the queue there drops it
    // for good: never sent, never retried, and `save()` resolved on the shared
    // drain as though it had worked.
    const boom = vi.spyOn(server, "put").mockRejectedValueOnce(new Error("access is denied"));

    await Promise.all([save({ theme: "light" }), save({ voice_input: true })]);
    boom.mockRestore();

    const both: WorkbenchSettings = { theme: "light", office_native: "auto", voice_input: true };
    // The queued document carries the refused body's fields too (every patch
    // merges onto `desired`), so sending it recovers both clicks — which is why
    // there is nothing left to report.
    expect(onDisk()).toEqual(both);
    expect(shown()).toEqual(both);
    expect(useSettings.getState().error).toBeNull();
  });

  it("shows what did land when the failure is the second write of a burst", async () => {
    // The mirror image: the first PUT lands, the one queued behind it does not.
    // The panel may not go on showing the document from before the burst — the
    // file has moved, and a stale panel misreports it as confidently as any race.
    //
    // The one test here that is a guard rather than a repro. Rendering every
    // reply used to give this away for free; withholding the superseded ones is
    // what puts it at risk, so it is pinned before the next reader is tempted to
    // drop a held answer instead of showing it.
    server.putDelay = [20, 0];
    const real = server.put;
    let turn = 0;
    const boom = vi.spyOn(server, "put").mockImplementation(async (body) => {
      turn += 1;
      if (turn === 2) throw new Error("access is denied");
      return real(body);
    });

    await Promise.all([save({ theme: "light" }), save({ voice_input: true })]);
    boom.mockRestore();

    expect(useSettings.getState().error).toBe("access is denied");
    expect(shown()).toEqual({ theme: "light", office_native: "auto", voice_input: false });
    expect(window_.theme).toBe("light");
  });
});

describe("a reply that a queued document already replaced", () => {
  it("is not rendered on the way to the one that won", async () => {
    server.putDelay = [30, 0];
    const seen: (WorkbenchSettings | null)[] = [];
    const stop = useSettings.subscribe((next) => seen.push(next.state?.stored ?? null));

    // Dark is what the window is already wearing, so light -> dark is a burst
    // whose *end state* is indistinguishable from having done nothing. What it
    // must not do is go through light on the way.
    await Promise.all([save({ theme: "light" }), save({ theme: "dark" })]);
    stop();

    expect(shown()?.theme).toBe("dark");
    expect(window_.theme).toBe("dark");
    expect(window_.worn).toEqual([]); // never repainted, not repainted twice
    expect(seen.some((stored) => stored?.theme === "light")).toBe(false);
  });
});

describe("the dock going away", () => {
  it("silences the answers without cancelling the writes", async () => {
    server.putDelay = [30, 0];
    const writes = Promise.all([save({ theme: "light" }), save({ voice_input: true })]);

    // Teardown with one PUT in flight and one document queued behind it. The
    // queued one is the user's change; `settings.json` outlives the panel.
    settingsTool.onDockReady?.(null);
    await writes;

    expect(onDisk()).toEqual({ theme: "light", office_native: "auto", voice_input: true });
    expect(useSettings.getState().state).toBeNull(); // and nothing landed on the reset store
  });

  it("does not leave the panel that comes back waiting on it", async () => {
    server.putDelay = [30, 0];
    const writes = Promise.all([save({ theme: "light" }), save({ voice_input: true })]);
    settingsTool.onDockReady?.(null);

    // A read stands down behind a drain that will paint the store. This one
    // will not — it lost its ticket — so standing behind it is "Loading…" for
    // good. It waits for the drain and then reads, never across it.
    await refresh();
    await writes;

    expect(shown()).toEqual({ theme: "light", office_native: "auto", voice_input: true });
  });
});
