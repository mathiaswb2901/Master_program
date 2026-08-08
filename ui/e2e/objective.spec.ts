/**
 * Journey — an objective is closed by evidence, not opinion (plan §3).
 *
 * The feature end to end, through the production wiring: a named session is given
 * a goal, its status is `open` with no evidence, a `ValidationResult` is driven
 * against *that objective* the way the server really mints one
 * (`POST /api/validation/run`), and only once a human **approves** the result
 * does the objective flip to `met`. A second session's objective, validated
 * against nothing, stays `open` throughout — two goals, independent (plural).
 *
 * Every mutating call runs inside the page so it rides the same-origin `/api`
 * proxy and carries the launch token (the token endpoint is exempt); enforcement
 * is on, so a tokenless POST would 401. The last step renders the result in the
 * Objectives panel, so the badge is exercised, not just the derived status.
 */

import { expect, test, type Page } from "@playwright/test";

import { openApp } from "./app";

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

interface Flow {
  firstId: string;
  secondId: string;
  openStatus: string; // first objective before approval
  metStatus: string; // first objective after approval
  secondStatus: string; // second objective, never validated
}

/** The whole REST flow in one authed page context: create two sessions, give
 * each a goal, validate + approve the first, and read back the derived statuses.
 * Returns the ids and the three statuses the assertions check. */
async function driveFlow(page: Page, root: string): Promise<Flow> {
  return page.evaluate(async (root) => {
    const { token } = (await (await fetch("/api/auth/token")).json()) as { token: string };
    const call = async (path: string, init: RequestInit = {}): Promise<unknown> => {
      const res = await fetch(path, {
        ...init,
        headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
      });
      if (!res.ok) throw new Error(`${path} -> ${String(res.status)}`);
      return res.status === 204 ? null : res.json();
    };

    const mk = async (name: string): Promise<string> => {
      const s = (await call("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ name, workspace: root }),
      })) as { id: string };
      return s.id;
    };
    const firstId = await mk("objective worker");
    const secondId = await mk("other worker");

    await call(`/api/sessions/${firstId}/objective`, {
      method: "PUT",
      body: JSON.stringify({ statement: "Reconcile Åsen 2 revenue" }),
    });
    await call(`/api/sessions/${secondId}/objective`, {
      method: "PUT",
      body: JSON.stringify({ statement: "A different, unvalidated goal" }),
    });

    // Validate the first objective. The subject is this objective — kind plus the
    // session id, the ref the server joins on. The one registered check judges it.
    const run = (await call("/api/validation/run", {
      method: "POST",
      body: JSON.stringify({
        subject: { kind: "objective", ref: firstId, label: "Reconcile Åsen 2 revenue" },
        checks: [],
      }),
    })) as { validation_id: string };

    // Evidence exists but no human has signed off — still open.
    const before = (await call(`/api/sessions/${firstId}/objective`)) as { status: string };

    // Approve — the one mandatory human decision — and it flips to met.
    await call(`/api/validation/${run.validation_id}/approve`, {
      method: "POST",
      body: JSON.stringify({ approver: "you" }),
    });
    const after = (await call(`/api/sessions/${firstId}/objective`)) as { status: string };
    const other = (await call(`/api/sessions/${secondId}/objective`)) as { status: string };

    return {
      firstId,
      secondId,
      openStatus: before.status,
      metStatus: after.status,
      secondStatus: other.status,
    };
  }, root);
}

/** Open the Objectives panel the way a user reaches a tool not on screen: the
 * QuickBar, through the registry (never a hardcoded button). */
async function openObjectivesPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">Objectives");
  await quickbar.locator(".wb-qb-row", { hasText: "Objectives" }).first().click();
  await expect(page.locator(".wb-objective")).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

test("an objective flips to met only on an approved result; a second stays open", async ({
  page,
}) => {
  await openApp(page);
  const root = ((await (await page.request.get("/api/workspace")).json()) as { root: string }).root;

  const flow = await driveFlow(page, root);

  await test.step("the derived status is the honest, evidence-backed answer", () => {
    // The one registered check (the reconciliation gate, handed no spec) returns a
    // medium-or-worse finding, so before any human sign-off the objective is *not*
    // met — it reads the unapproved evidence as at-risk/failing, never a silent
    // green and never still "open" once evidence exists.
    expect(["at-risk", "failing"]).toContain(flow.openStatus);
    // Approval closes it — the plan's gate.
    expect(flow.metStatus).toBe("met");
    // The second objective, validated against nothing, never moved (plural).
    expect(flow.secondStatus).toBe("open");
  });

  await test.step("the Objectives panel renders the met badge, and the other as open", async () => {
    await openObjectivesPanel(page);
    const metRow = page.locator(".wb-objective-row", { hasText: "Reconcile Åsen 2 revenue" });
    const openRow = page.locator(".wb-objective-row", { hasText: "A different, unvalidated goal" });
    await expect(metRow).toBeVisible();
    await expect(metRow.locator(".wb-pill")).toContainText("Met");
    await expect(openRow.locator(".wb-pill")).toContainText("Open");
  });

  // Put the arrangement back for whatever journey shares this workspace next.
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});
