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
 *     a single token, and with `WORKBENCH_CLAUDE_PROJECTS_DIR` pointed at the
 *     workspace's `-projects` sibling — seeded with journey 11's conversations,
 *     and outside the workspace so the developer's real session history is
 *     never read *and* the fixture is never a folder in the file tree.
 *     Everything else is production code: real files on disk, the real
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

import {
  E2E_APP_DATA,
  E2E_AUTH_TOKEN,
  E2E_WORKSPACE,
  projectsDirFor,
  WORKSPACE_ENV,
  worktreeRootFor,
} from "./e2e/workspace";

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

/**
 * Dedicated ports so a running dev server (8787/5173) never collides.
 *
 * Overridable because this repo's own workflow runs several lanes at once, each
 * in its own worktree — and two suites are otherwise a port collision that
 * refuses to start (`reuseExistingServer: false`, correctly: a suite that
 * attached to *another lane's* server would assert against another lane's code).
 * Each run already gets its own temp workspace, so a second pair of ports is the
 * only thing standing between two lanes and a clean parallel run:
 *
 *     WB_E2E_SERVER_PORT=8790 WB_E2E_UI_PORT=4175 npm run e2e
 *
 * Defaults are unchanged, so CI and a plain `npm run e2e` behave exactly as
 * before. These are `WB_E2E_*` — the suite's own knobs, deliberately outside the
 * `WORKBENCH_*` prefix stripped above, which is the server's configuration.
 */
const port = (name: string, fallback: number): number => {
  const raw = process.env[name];
  const parsed = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isInteger(parsed) && parsed > 0 && parsed < 65_536 ? parsed : fallback;
};
const SERVER_PORT = port("WB_E2E_SERVER_PORT", 8788);
const UI_PORT = port("WB_E2E_UI_PORT", 4173);
const SERVER_URL = `http://127.0.0.1:${SERVER_PORT}`;

// The per-launch auth token, now *enforced* by default (M5 item 8, PR4). The
// app itself bootstraps it over `GET /api/auth/token`, but specs also reach the
// API out of band through `page.request.*` (e.g. reading `/api/layouts` to
// assert what persisted) — a context the app's token never rides. Pinning the
// token to a known value lets the harness attach it as a default header
// (`extraHTTPHeaders` below) so those calls authenticate, while the app still
// exercises the real bootstrap + WS-subprotocol paths on its own traffic.
// Declared in `e2e/workspace.ts` so `evidence-persist.spec.ts` — which starts a
// second backend the browser context also talks to — pins the same one.
const AUTH_TOKEN = E2E_AUTH_TOKEN;

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
    // Out-of-band `page.request.*` calls the specs make to inspect server state
    // authenticate with the same token the server was launched with; the app's
    // own REST/WS traffic carries its own token independently (token.ts/ws.ts).
    extraHTTPHeaders: { "X-Workbench-Token": AUTH_TOKEN },
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
        // Pin the enforced token to the value the harness attaches out of band
        // (see AUTH_TOKEN / extraHTTPHeaders above). Enforcement stays ON — the
        // shipped default — so this run really is the run a user gets.
        WORKBENCH_AUTH_TOKEN: AUTH_TOKEN,
        WORKBENCH_FAKE_AGENT: "1",
        // The host backend's counterpart to fake-agent mode: the whole Office
        // host lifecycle, its refusals and its fallbacks, with no Office, no
        // Rust and no native window anywhere near CI. Journey 7 drives it.
        WORKBENCH_OFFICE_FAKE: "1",
        // The voice seam's counterpart to both of the above: the whole
        // push-to-talk lifecycle — start, audio chunks, interim text, a final
        // transcript — with no microphone, no model and no audio hardware.
        // Journey `voice.spec.ts` drives it, and the browser side pairs it with
        // the scripted capture in `ui/src/voiceCapture.ts` (silence on a timer),
        // so a headless runner really can hold a push-to-talk button down.
        WORKBENCH_VOICE_FAKE: "1",
        // The fourth of the same family (M6 staged review PR1): the toolchain
        // gate's whole flow — slot resolution, the fingerprint, the bounded log,
        // the payload route and the expander — with no ruff, no pytest and no
        // npm anywhere near CI. Journey `review.spec.ts` drives it.
        WORKBENCH_GATE_FAKE: "1",
        WORKBENCH_PORT: String(SERVER_PORT),
        WORKBENCH_WORKSPACE_ROOT: E2E_WORKSPACE,
        // A *sibling* of the workspace, seeded by `e2e/workspace.ts`: pointing
        // it away from `~/.claude` is what keeps the developer's real history
        // unread, and keeping it out of the workspace is what stops journey
        // 11's fixture becoming a folder in journey 1's file tree.
        WORKBENCH_CLAUDE_PROJECTS_DIR: projectsDirFor(E2E_WORKSPACE),
        // Another sibling, and for the same two reasons: a pooled worktree
        // inside the workspace would appear in journey 1's file tree, and the
        // shipped default (`%LOCALAPPDATA%`) would leave a real checkout per
        // E2E run on the developer's machine. Mission Control's workers each
        // borrow a slot from here (journey 12).
        WORKBENCH_WORKTREE_ROOT: worktreeRootFor(E2E_WORKSPACE),
        // Machine-local state — today the recent-workspaces list. Pointed at a
        // per-run sibling for the same reason the projects dir is: the switcher
        // journey both reads and writes this, and it must never be the
        // developer's own history. Outside the workspace, because the pool
        // root's rule applies here too — state about the user is not a file in
        // one of their projects.
        WORKBENCH_APP_DATA_ROOT: E2E_APP_DATA,
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
