/**
 * The **perf lane**: the production build, a real backend, a 5,005-file
 * workspace, and budgets asserted as tests.
 *
 * Its own config rather than another project inside `playwright.config.ts`,
 * for three reasons that are all structural:
 *
 *  1. **A different workspace.** `webServer` is config-level in Playwright, and
 *     the backend under measurement must be launched *in the perf fixture* —
 *     the eight journeys run in a small seeded temp directory, which is exactly
 *     the workspace where none of these problems show up.
 *  2. **Different reporting.** Perf output is a report to compare against, not
 *     a pass/fail line; it lands in `playwright-report-perf/` and
 *     `perf-results/` so a perf run never overwrites a journey run's report.
 *  3. **Different retry policy.** See `retries` below.
 *
 * Which assertions block is decided in CI (`.github/workflows/ci.yml`), by tag:
 * everything without `@wallclock` counts work and cannot flake, so it blocks;
 * `@wallclock` measures time on a shared runner, so it is recorded and does not.
 * The tag is on the test, in the spec, next to the number it asserts.
 */

import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

import { PERF_WORKSPACE, REPO_ROOT } from "./e2e/perf/fixture";

declare const process: { env: Record<string, string | undefined> };

/**
 * Same rule as `playwright.config.ts`: the backend under measurement is
 * configured by this file and nothing else. A developer's exported
 * `WORKBENCH_ONLYOFFICE_URL` would put a Document Server in the launch path and
 * change the number this lane exists to defend.
 */
for (const key of Object.keys(process.env)) {
  if (key.startsWith("WORKBENCH_")) delete process.env[key];
}

/** Dedicated ports: neither a dev server (8787/5173) nor the journey suite
 * (8788/4173) may collide with a perf run. */
const SERVER_PORT = 8790;
const UI_PORT = 4175;
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;

/** Session storage lives *outside* the fixture on purpose: inside, it would be
 * a directory the file tree walks and the folder list reports, so the server
 * would be measuring a workspace it had just changed. */
const PROJECTS_DIR = `${PERF_WORKSPACE}-projects`;

export default defineConfig({
  testDir: "./e2e/perf",
  fullyParallel: false,
  workers: 1,
  forbidOnly: process.env.CI !== undefined,
  // A retry cannot rescue a count — those are deterministic, and a second run
  // of a failing budget fails the same way. It exists for the `@wallclock`
  // journeys, where a runner hiccup (a cold page load racing a busy host) is
  // noise rather than a regression, and for them a retry is honest because
  // their numbers are reported rather than enforced.
  retries: process.env.CI !== undefined ? 1 : 0,
  // The watcher budget makes 20 round-trips through disk -> watcher -> UI.
  timeout: 240_000,
  expect: { timeout: 30_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report-perf" }],
    ["json", { outputFile: "perf-results/results.json" }],
  ],
  outputDir: "perf-results/artifacts",
  use: {
    baseURL: `http://127.0.0.1:${UI_PORT}`,
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [{ name: "perf", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `uv run --project "${REPO_ROOT}" workbench-server`,
      cwd: PERF_WORKSPACE,
      env: {
        WORKBENCH_PORT: String(SERVER_PORT),
        WORKBENCH_WORKSPACE_ROOT: PERF_WORKSPACE,
        WORKBENCH_CLAUDE_PROJECTS_DIR: path.join(PROJECTS_DIR, "projects"),
        WORKBENCH_LOG_LEVEL: "warning",
      },
      url: `${SERVER_URL}/api/health`,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 120_000,
    },
    {
      command: `npm run preview -- --host 127.0.0.1 --port ${UI_PORT} --strictPort`,
      env: { WORKBENCH_SERVER_URL: SERVER_URL },
      url: `http://127.0.0.1:${UI_PORT}/`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
