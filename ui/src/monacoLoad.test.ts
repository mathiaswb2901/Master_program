/**
 * *When* `loadMonaco` reads the theme — which is the whole of it.
 *
 * Monaco arrives on its own chunk, warmed on an idle callback, so there is a
 * window between "the load is scheduled" and "the bundle has evaluated" in
 * which `defineWorkbenchTheme` cannot do anything: there is no editor to define
 * a theme on. A theme captured when the load was *scheduled* is therefore stale
 * by the time it is used, while the CSS tokens it reads are live — so the theme
 * *named* `workbench-light` would be defined with dark colors and
 * `workbench-dark`, the name `<Editor theme=…>` goes on to ask for, would never
 * be defined at all. Monaco substitutes a built-in theme for a name it does not
 * know, without a word.
 *
 * `e2e/theme.spec.ts` is the reproduction, in a browser, on the real chunk.
 * This is the same contract asserted on `monaco.ts` alone, in milliseconds, so
 * that reintroducing the parameter fails immediately and by name.
 *
 * The bundle and the two DOM reads on this path are hand-stubbed: the unit
 * suite runs on the node environment on purpose (`src/test-setup.ts`), and
 * neither a jsdom dependency nor 3.3 MB of editor buys this assertion anything.
 */

import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { defineTheme } = vi.hoisted(() => ({
  defineTheme: vi.fn<(name: string, theme: unknown) => void>(),
}));

vi.mock("./monacoBundle", () => ({
  configureBundle: () => ({ editor: { defineTheme } }),
}));

/** `data-theme` on `<html>` — what `documentTheme()` reads. */
let attribute: string | null = null;
/** Idle callbacks `prefetchMonaco` armed, fired by hand. */
const idle: Array<() => void> = [];

beforeEach(() => {
  vi.resetModules(); // `monaco.ts` memoizes its load in module state
  defineTheme.mockClear();
  attribute = null;
  idle.length = 0;
  vi.stubGlobal("document", {
    documentElement: {
      getAttribute: (name: string) => (name === "data-theme" ? attribute : null),
    },
  });
  // A real hex keeps `toHex` on its normal branch; which color is irrelevant.
  vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: () => "#1A1D22" }));
  vi.stubGlobal("requestIdleCallback", (cb: () => void) => idle.push(cb));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Theme names Monaco was told to define, in call order. */
const defined = (): string[] => defineTheme.mock.calls.map(([name]) => name);

it("defines the theme current when the bundle lands, not when the load was armed", async () => {
  const { loadMonaco, prefetchMonaco } = await import("./monaco");

  attribute = "light";
  prefetchMonaco(); // armed while the app is light…
  attribute = null; // …and toggled to dark before the callback fires
  for (const run of idle) run();
  await loadMonaco();

  expect(defined()).toEqual(["workbench-dark"]);
});

it("defines nothing before the bundle lands, and loses no toggle by it", async () => {
  const { defineWorkbenchTheme, loadMonaco, prefetchMonaco } = await import("./monaco");

  attribute = "light";
  prefetchMonaco();
  attribute = null;
  // What `store.setTheme` does on a toggle. It can only no-op here — the point
  // is that the load below picks the toggle up rather than overwriting it.
  defineWorkbenchTheme("dark");
  expect(defined()).toEqual([]);

  for (const run of idle) run();
  await loadMonaco();

  expect(defined()).toEqual(["workbench-dark"]);
});

it("loads once however many callers ask, and defines the theme once", async () => {
  const { loadMonaco, prefetchMonaco } = await import("./monaco");

  prefetchMonaco();
  for (const run of idle) run();
  const [first, second] = await Promise.all([loadMonaco(), loadMonaco()]);

  expect(first).toBe(second);
  expect(defined()).toEqual(["workbench-dark"]);
});
