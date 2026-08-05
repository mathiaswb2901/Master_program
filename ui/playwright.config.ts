/**
 * Playwright E2E: the built UI driven against the real backend.
 *
 * The flow `npm run e2e` runs, in order:
 *
 *  1. `npm run build` — type-check + bundle into `ui/dist`. The suite never
 *     runs against the Vite dev server: what ships is what is tested.
 *  2. `uv run workbench-server` in a **per-run temp workspace** (see
 *     `e2e/workspace.ts`), with `WORKBENCH_FAKE_AGENT=1` so chat, tool rows,
 *     permission prompts and plan cards are exercised without a Claude login or
 *     a single token, and with `WORKBENCH_CLAUDE_PROJECTS_DIR` pointed inside
 *     that workspace so the developer's real session history is never read or
 *     listed. Everything else is production code: real files on disk, the real
 *     watcher, real ConPTY terminals, real WebSockets.
 *  3. `vite preview` serving `ui/dist`, proxying `/api` and `/ws` to that
 *     backend (see `vite.config.ts` — the proxy target follows
 *     `WORKBENCH_SERVER_URL`, which is set below).
 *
 * Both servers are started by Playwright and torn down with it. The suite runs
 * single-worker on purpose: one backend, one workspace on disk, one PTY host —
 * parallel journeys would be sharing all three.
 *
 * Nothing here sleeps. Journeys wait on the app's own signals (a rendered tab,
 * a settled tool row, a watcher round-trip) through Playwright's auto-waiting.
 */

import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig, devices } from "@playwright/test";

import { E2E_WORKSPACE, WORKSPACE_ENV } from "./e2e/workspace";

// The E2E harness is deliberately outside the `tsc -b` program (tsconfig.json
// covers `src` + the two vite configs), so node's globals need no types
// package. `process` is the only one used here.
declare const process: { env: Record<string, string | undefined> };

/**
 * The backend under test is configured by this file and by nothing else.
 *
 * Playwright spawns `webServer` with `{...process.env, ...env}`, so every
 * `WORKBENCH_*` variable in the developer's shell reconfigures the server the
 * suite is asserting against. That is not hypothetical: CLAUDE.md tells this
 * project's developers to export `WORKBENCH_ONLYOFFICE_URL` and
 * `WORKBENCH_ONLYOFFICE_JWT_SECRET` for Office editing, which turns Office on
 * and makes journey 7 wait for a degraded card the app is right not to render.
 * `WORKBENCH_INHERIT_USER_SETTINGS` would go further and pull the developer's
 * global hooks and permission rules into the sessions under test.
 *
 * Dropping the whole prefix makes a run on a working machine identical to a run
 * on a bare CI runner — the difference between the two is exactly the class of
 * bug an E2E gate exists to catch, so it must not be a source of them. The
 * workspace handoff uses the `WB_E2E_` prefix and is untouched.
 */
for (const key of Object.keys(process.env)) {
  if (key.startsWith("WORKBENCH_")) delete process.env[key];
}

/** Repo root: this file lives in `<root>/ui`. Resolved (no trailing separator,
 * which would escape the closing quote of the command below on Windows). */
const REPO_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..");

/** Dedicated ports so a running dev server (8787/5173) never collides. */
const SERVER_PORT = 8788;
const UI_PORT = 4173;
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;

export default defineConfig({
  testDir: "./e2e",
  // `e2e/perf/` is the Feel perf lane and belongs to `playwright.perf.config.ts`
  // — a different workspace (the generated 5,005-file fixture), different ports
  // and a different report. Without this it is picked up here too, and its
  // budgets are asserted against the small seeded journey workspace, where they
  // measure nothing and fail on a missing directory.
  testIgnore: "perf/**",
  // Same split as the perf config: `.spec.ts` is a browser journey, `.test.ts`
  // under `e2e/` is a vitest unit test over the harness itself. Playwright's
  // default `testMatch` would take both.
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: process.env.CI !== undefined,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${UI_PORT}`,
    trace: "retain-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      // `--project` points uv at the repo without changing the working
      // directory, so the server's CWD really is the temp workspace — the way a
      // user launches it. WORKBENCH_WORKSPACE_ROOT states the same thing
      // explicitly: a suite that wrote into the repo checkout because CWD
      // resolution surprised us would be worse than a suite that fails.
      command: `uv run --project "${REPO_ROOT}" workbench-server`,
      cwd: E2E_WORKSPACE,
      env: {
        [WORKSPACE_ENV]: E2E_WORKSPACE,
        WORKBENCH_FAKE_AGENT: "1",
        // The host backend's counterpart to fake-agent mode: the whole Office
        // host lifecycle, its refusals and its fallbacks, with no Office, no
        // Rust and no native window anywhere near CI. Journey 7 drives it.
        WORKBENCH_OFFICE_FAKE: "1",
        WORKBENCH_PORT: String(SERVER_PORT),
        WORKBENCH_WORKSPACE_ROOT: E2E_WORKSPACE,
        WORKBENCH_CLAUDE_PROJECTS_DIR: path.join(E2E_WORKSPACE, ".claude-projects"),
        WORKBENCH_LOG_LEVEL: "warning",
      },
      url: `${SERVER_URL}/api/health`,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      timeout: 120_000,
    },
    {
      // --host: `vite preview` binds to `localhost`, which on Windows resolves
      // to ::1 only — the readiness probe and the browser both use IPv4 here.
      command: `npm run preview -- --host 127.0.0.1 --port ${UI_PORT} --strictPort`,
      env: { WORKBENCH_SERVER_URL: SERVER_URL },
      url: `http://127.0.0.1:${UI_PORT}/`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
