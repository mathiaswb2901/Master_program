/**
 * Journey — **a result you can restart into** (productivity loops, PR-C).
 *
 * The reproduction first, and it is a real one: on master a validation lives in
 * an in-memory LRU, so the moment the server process ends the proof is gone.
 * Somebody ran the checks, somebody signed off on them, and a restart leaves the
 * Review panel with nothing to show for either. This journey does exactly what a
 * user does — run it, approve it, close the app, open it again — and asserts the
 * result, its evidence and its approval are still there.
 *
 * **Why this journey brings its own server.** Every other spec drives the one
 * backend Playwright manages; killing that mid-suite would take every journey
 * after it down with it. So this one starts a *second* Workbench on its own
 * port, in its own temp workspace, with its own app-data root — and because the
 * server mounts the built `ui/dist` at `/` when it is there (and `npm run e2e`
 * has just built it), the page under test is the same production bundle, served
 * same-origin, with no proxy in the way. Killing and restarting that process is
 * the real thing, and it costs the shared suite nothing.
 *
 * The auth token is the harness's own (`E2E_AUTH_TOKEN`): the browser context
 * attaches it to every request as an extra header, so the second server has to
 * be launched with the same one or the app's own traffic would be refused.
 */

import child_process from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { E2E_AUTH_TOKEN } from "./workspace";

declare const process: {
  env: Record<string, string | undefined>;
  platform: string;
  on: (event: string, listener: () => void) => void;
};

/** Repo root: this file lives in `<root>/ui/e2e`. */
const REPO_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..");

/**
 * A port of this journey's own, derived from the suite's so two lanes running
 * side by side (`WB_E2E_SERVER_PORT=8790 npm run e2e`) stay disjoint here too.
 */
const basePort = (): number => {
  const raw = process.env.WB_E2E_SERVER_PORT;
  const parsed = raw === undefined ? Number.NaN : Number.parseInt(raw, 10);
  return Number.isInteger(parsed) && parsed > 0 && parsed < 65_000 ? parsed : 8788;
};
const PORT = basePort() + 3;
const BASE = `http://127.0.0.1:${String(PORT)}`;

/** This journey's workspace, its app data and its worktree pool — all siblings,
 * all temporary, none of them the suite's. Left behind on failure like the
 * suite's own workspace, for the same reason: it holds the exact state. */
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), "workbench-evidence-"));
const WORKSPACE = path.join(ROOT, "project");
const APP_DATA = path.join(ROOT, "app-data");

function seedWorkspace(): void {
  fs.mkdirSync(path.join(WORKSPACE, ".workbench"), { recursive: true });
  fs.mkdirSync(APP_DATA, { recursive: true });
  // Seeded dismissed, exactly as `e2e/workspace.ts` does: a fresh temp folder is
  // a window nobody has arranged, and the welcome card and Setup tab would open
  // themselves over the panel this journey is about.
  for (const name of ["welcome.json", "setup.json"]) {
    fs.writeFileSync(
      path.join(WORKSPACE, ".workbench", name),
      `${JSON.stringify({ dismissed: true }, null, 2)}\n`,
      "utf-8",
    );
  }
  fs.writeFileSync(
    path.join(WORKSPACE, "se3-dispatch.xlsx.notes.md"),
    "# The workbook this journey pretends to reconcile\n",
    "utf-8",
  );
}

const SERVER_ENV: Record<string, string> = {
  WORKBENCH_AUTH_TOKEN: E2E_AUTH_TOKEN,
  WORKBENCH_PORT: String(PORT),
  WORKBENCH_WORKSPACE_ROOT: WORKSPACE,
  WORKBENCH_APP_DATA_ROOT: APP_DATA,
  WORKBENCH_WORKTREE_ROOT: path.join(ROOT, "worktrees"),
  WORKBENCH_CLAUDE_PROJECTS_DIR: path.join(ROOT, "projects"),
  WORKBENCH_FAKE_AGENT: "1",
  WORKBENCH_GATE_FAKE: "1",
  WORKBENCH_LOG_LEVEL: "warning",
};

let server: child_process.ChildProcess | null = null;

async function healthy(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE}/api/health`);
    return res.ok;
  } catch {
    return false;
  }
}

/** Poll a predicate on a bounded budget. Nothing here sleeps its way to a pass:
 * a server that never comes up fails the step rather than the next assertion. */
async function until(what: () => Promise<boolean>, budgetMs: number): Promise<boolean> {
  const deadline = Date.now() + budgetMs;
  while (Date.now() < deadline) {
    if (await what()) return true;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  return false;
}

async function startServer(): Promise<void> {
  server = child_process.spawn(
    `uv run --project "${REPO_ROOT}" workbench-server`,
    { cwd: WORKSPACE, env: { ...process.env, ...SERVER_ENV }, shell: true, stdio: "pipe" },
  );
  expect(await until(healthy, 120_000), `the second server never answered on ${BASE}`).toBe(true);
}

/** Kill the process *tree*: `uv run` is a launcher and the interpreter holding
 * the port is its child, so killing the parent alone leaves the port held. */
function killTree(child: child_process.ChildProcess): void {
  if (child.pid === undefined) return;
  if (process.platform === "win32") {
    child_process.spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
      stdio: "ignore",
    });
  } else {
    child.kill("SIGKILL");
  }
}

async function stopServer(): Promise<void> {
  if (server === null) return;
  killTree(server);
  server = null;
  // The port really has to be free before the replacement claims it.
  expect(await until(async () => !(await healthy()), 30_000), "the server would not die").toBe(
    true,
  );
}

test.beforeAll(async () => {
  seedWorkspace();
  await startServer();
});

test.afterAll(async () => {
  await stopServer();
});

// A hard stop for the case where a worker dies mid-test: the child is spawned
// detached from Playwright's own supervision, so nothing else would reap it.
process.on("exit", () => {
  if (server !== null) killTree(server);
});

interface ValidationSubject {
  kind: "session_output" | "file" | "objective";
  ref: string;
  label: string;
}

/** Mint a real result through the second server, from inside the page — so it
 * rides the same-origin `/api` and carries the launch token, exactly as the app
 * does (`review.spec.ts`'s pattern). */
async function seedValidation(page: Page, subject: ValidationSubject): Promise<string> {
  return page.evaluate(async (subject) => {
    const tokenRes = await fetch("/api/auth/token");
    const { token } = (await tokenRes.json()) as { token: string };
    const res = await fetch("/api/validation/run", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
      body: JSON.stringify({ subject, checks: [], params: {} }),
    });
    if (!res.ok) throw new Error(`run failed: ${String(res.status)}`);
    const result = (await res.json()) as { validation_id: string };
    return result.validation_id;
  }, subject);
}

/** Open the Review panel the way a user reaches a tool not on screen. */
async function openReviewPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">Review validation");
  await quickbar.locator(".wb-qb-row", { hasText: "Review validation" }).first().click();
  await expect(page.locator(".wb-review").first()).toBeVisible();
}

/** Pane ids as they are on disk — the strings the next launch will trust
 * (`review.spec.ts`'s helper, aimed at this journey's own server). */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get(`${BASE}/api/layouts`)).json()) as {
    state: { current: { panels?: Record<string, unknown> } | null };
  };
  return Object.keys(response.state.current?.panels ?? {}).sort();
}

const SUBJECT: ValidationSubject = {
  kind: "file",
  ref: "se3-dispatch.xlsx",
  label: "SE3 dispatch reconciliation",
};

test("a result, its evidence and its approval survive a server restart", async ({ page }) => {
  await page.goto(`${BASE}/`);
  await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();

  const validationId = await test.step("run a validation and approve it", async () => {
    const id = await seedValidation(page, SUBJECT);
    expect(id).toMatch(/^val_/);

    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: SUBJECT.label }).click();
    const body = page.locator(`.wb-review-body[data-validation="${id}"]`);
    await expect(body).toBeVisible();
    // Not blank: the registered checks produced evidence, which is the thing a
    // restart has to bring back.
    await expect(body.locator(".wb-evidence")).not.toHaveCount(0);

    await body.locator(".wb-review-note").fill("checked the four flagged hours by hand");
    await body.locator(".wb-review-approve").click();
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
    return id;
  });

  await test.step("the pane is really on disk, so the next launch restores it", async () => {
    // The arrangement is what makes the reload below a *cold launch* rather than
    // a re-click: the restored pane is bound to this `validation_id` by the
    // string in `layouts.json`, and it has to resolve against a store the server
    // rebuilt from disk. Polled, because the autosave is debounced.
    await expect
      .poll(() => persistedPaneIds(page), { timeout: 10_000 })
      .toContain(`review#${validationId}`);
  });

  const before = await test.step("what the server holds, before the restart", async () => {
    return page.evaluate(async () => {
      const tokenRes = await fetch("/api/auth/token");
      const { token } = (await tokenRes.json()) as { token: string };
      const res = await fetch("/api/validation", { headers: { "X-Workbench-Token": token } });
      return (await res.json()) as {
        results: {
          validation_id: string;
          risk: string;
          evidence: { label: string; outcome: string }[];
          approval: { approver: string; note: string | null } | null;
        }[];
      };
    });
  });
  const mine = before.results.find((r) => r.validation_id === validationId);
  expect(mine, "the run this journey made is not in the snapshot").toBeDefined();

  await test.step("close the app: the server process really ends", async () => {
    await stopServer();
    expect(await healthy()).toBe(false);
  });

  await test.step("open it again — nothing but a reconnect", async () => {
    await startServer();
    await page.reload();
    await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
  });

  await test.step("the Review panel shows what was proven, and who approved it", async () => {
    // No client action but the reconnect: the saved arrangement restores the
    // pane bound to this id, and the pane resolves it against a store the new
    // process rebuilt from `.workbench/validation/`.
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body).toBeVisible();
    // …and the index pane beside it lists the run again.
    await expect(page.locator(".wb-review-row", { hasText: SUBJECT.label })).toBeVisible();
    // Same risk, same evidence — line for line, not merely the same count —
    // and the same approval, with its note.
    await expect(body).toHaveAttribute("data-risk", mine?.risk ?? "");
    await expect(body.locator(".wb-evidence-label")).toHaveText(
      (mine?.evidence ?? []).map((item) => item.label),
    );
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
    await expect(body.locator(".wb-review-approved")).toContainText("flagged hours");
    // …and no tombstone: this pane is a view onto a result the server still has.
    await expect(page.locator(".wb-review-tombstone")).toHaveCount(0);
  });

  await test.step("and the proof is handable: Export writes a one-page report", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await body.locator(".wb-review-export-run").click();
    const report = body.locator(".wb-review-export-path");
    await expect(report).toContainText(".workbench/validation/exports");

    // The file is really on disk, and it names the subject, the risk, the
    // evidence and the approver — the four things somebody who was not there
    // needs before they can act on it.
    const written = path.join(
      WORKSPACE,
      ".workbench",
      "validation",
      "exports",
      `${validationId}.md`,
    );
    expect(fs.existsSync(written), `no export at ${written}`).toBe(true);
    const markdown = fs.readFileSync(written, "utf-8");
    expect(markdown).toContain(SUBJECT.label);
    expect(markdown).toContain(validationId);
    expect(markdown).toContain("Approval");
    expect(markdown).toContain("checked the four flagged hours by hand");
    // The two things PR-C cannot know yet say so out loud rather than going
    // missing (AXI shape 2, applied to a human reader).
    expect(markdown).toContain("not run from a spec");
  });

  await test.step("…and from outside the window, the way a script would", async () => {
    // The other half of "handable": `validation.export` is a *registered*
    // command, so `workbench-cmd invoke validation.export` reaches it through
    // exactly this endpoint — token-gated by the middleware, and refused unless
    // the connected window published the id. Driven here as the CLI drives it
    // (`page.request` carries the launch token, not the page's own session), so
    // what is proven is the relay path and not a second in-page button.
    const listed = (await (await page.request.get(`${BASE}/api/commands`)).json()) as {
      commands: { id: string }[];
    };
    expect(listed.commands.map((entry) => entry.id)).toContain("validation.export");

    const written = path.join(
      WORKSPACE,
      ".workbench",
      "validation",
      "exports",
      `${validationId}.md`,
    );
    fs.rmSync(written, { force: true });

    const outcome = (await (
      await page.request.post(`${BASE}/api/commands/invoke`, {
        data: { command_id: "validation.export", params: {} },
      })
    ).json()) as { ok: boolean; detail: string };
    expect(outcome.ok, outcome.detail).toBe(true);
    // The file is back — written by the window, on an invocation that came from
    // outside it. And the app said so, rather than doing it silently.
    await expect
      .poll(() => fs.existsSync(written), { timeout: 10_000 })
      .toBe(true);
    await expect(page.locator(".wb-toast")).toContainText("Evidence exported to");

    // An id the window never published is refused, not guessed at.
    const refused = await page.request.post(`${BASE}/api/commands/invoke`, {
      data: { command_id: "validation.export.everything", params: {} },
    });
    expect(refused.status()).toBe(404);
  });
});
