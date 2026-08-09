/**
 * Journey — validation surfaces in the app (M6 PR3).
 *
 * The order is the feature. First the **quiet** bar: with nothing validated, the
 * status reading is absent (§6.7 — a quiet bar means nothing needs you). Then a
 * `ValidationResult` is driven into existence the way the server really mints
 * one — `POST /api/validation/run`. The reconciliation gate (#85) is now the one
 * registered check, so a run that names no check runs it; handed no spec, it
 * judges what it can and returns a `fail` line — a **high**-risk result, which is
 * a real end-to-end result the panel must surface. (The empty-evidence *blocked*
 * rendering is covered where it is deterministic — the unit suite's
 * `EvidenceGallery` case — since every registered check now emits evidence.)
 *
 * The Review panel then shows that result: the risk badge, and an evidence
 * gallery with the check's finding rather than blankness. A `high` result is
 * medium-or-worse, so it is **awaiting approval** — the one mandatory human
 * decision — and Approve records it and reflects the recorded
 * `ValidationApproval`.
 *
 * The POST runs inside the page so it rides the same-origin `/api` proxy and
 * carries the launch token (fetched from the token endpoint, which is exempt);
 * enforcement is on, so a tokenless POST would 401.
 */

import { expect, test, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { openApp } from "./app";

interface ValidationSubject {
  kind: "session_output" | "file" | "objective";
  ref: string;
  label: string;
}

const NO_LAYOUT = { current: null, current_name: null, saved: [] };

/** Pane ids as they are on disk — the strings the next launch will trust. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Wait until the debounced autosave has written a pane we just opened. */
async function persisted(page: Page, paneId: string): Promise<void> {
  await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(paneId);
}

// Each test starts from the default arrangement: the two-pane test opens instance
// panes that would otherwise be scenery in the single-result test, and the shared
// server holds both runs' validations in memory across tests.
test.beforeEach(async ({ page }) => {
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

/** Mint a real result through the server, from inside the page so the token
 * rides along. Returns its `validation_id`. */
async function seedValidation(
  page: Page,
  subject: ValidationSubject,
  checks: string[] = [],
): Promise<string> {
  return page.evaluate(
    async ({ subject, checks }) => {
      const tokenRes = await fetch("/api/auth/token");
      const { token } = (await tokenRes.json()) as { token: string };
      const res = await fetch("/api/validation/run", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
        body: JSON.stringify({ subject, checks, params: {} }),
      });
      if (!res.ok) throw new Error(`run failed: ${String(res.status)}`);
      const result = (await res.json()) as { validation_id: string; risk: string };
      return result.validation_id;
    },
    { subject, checks },
  );
}

/** Open the Review panel the way a user reaches a tool not on screen: the
 * QuickBar, through the registry (never a hardcoded panel button). */
async function openReviewPanel(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">Review validation");
  await quickbar.locator(".wb-qb-row", { hasText: "Review validation" }).first().click();
  await expect(page.locator(".wb-review")).toBeVisible();
}

test("validation surfaces: the quiet bar, the risk badge, the approval gate", async ({
  page,
}) => {
  await openApp(page);

  await test.step("with nothing validated, the status reading is absent (§6.7)", async () => {
    await expect(page.locator(".wb-review-status")).toHaveCount(0);
  });

  const subject: ValidationSubject = {
    kind: "session_output",
    ref: "sess-e2e",
    label: "Åsen 2 dispatch output",
  };

  await test.step("a run mints a result the registered check judged", async () => {
    const id = await seedValidation(page, subject);
    expect(id).toMatch(/^val_/);
  });

  await test.step("the status bar now carries a reading — one subject to review", async () => {
    const reading = page.locator(".wb-review-status");
    await expect(reading).toHaveCount(1);
    await expect(reading).toContainText("to review");
  });

  await test.step("the Review panel indexes the result and opens it", async () => {
    await openReviewPanel(page);
    // The bare pane is an index; the seeded result is a row, opened by click.
    const row = page.locator(".wb-review-row", { hasText: "Åsen 2 dispatch output" });
    await expect(row).toBeVisible();
    await row.click();
  });

  await test.step("the risk badge, and an evidence gallery carrying the check's finding", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body).toBeVisible();
    // The header badge is the result's risk — high, from the check's fail. It is
    // scoped to the header so it is not confused with an evidence-row pill.
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("High risk");
    // The gallery is not blank: the check produced at least one evidence row, and
    // its outcome renders as a Fail pill (the finding, surfaced — not a silent
    // green). We assert the UI shape, not the check's own wording.
    await expect(body.locator(".wb-evidence")).not.toHaveCount(0);
    await expect(body.locator(".wb-evidence .wb-pill").first()).toContainText("Fail");
  });

  await test.step("a high-risk result awaits a human, and Approve records the decision", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-note").fill("acknowledged: reviewed the finding");
    await body.locator(".wb-review-approve").click();
    // The recorded ValidationApproval, reflected: who and the note.
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
    await expect(body.locator(".wb-review-approved")).toContainText("acknowledged");
    // And no button remains — the decision is settled.
    await expect(body.locator(".wb-review-approve")).toHaveCount(0);
  });

  await test.step("with the one result approved, the bar goes quiet again", async () => {
    await expect(page.locator(".wb-review-status")).toHaveCount(0);
  });
});

/**
 * The plural-tool contract (CLAUDE.md product principle 4): two Review panes are
 * two independent reviews — bound to two different `validation_id`s — and they
 * stay that way **through a reload**, which is what proves the binding lives in
 * the pane id on disk rather than in memory. A reload is the real cold launch:
 * the layout restores two `review#<id>` panes before `GET /api/validation`
 * resolves, so it also exercises the hydration gate (a still-held result must
 * come back live, never as a flashed tombstone).
 */
test("two Review panes are independent, through a reload", async ({ page }) => {
  await openApp(page);

  const first: ValidationSubject = { kind: "session_output", ref: "sess-two-a", label: "First subject A" };
  const second: ValidationSubject = { kind: "file", ref: "wb-two-b.xlsx", label: "Second subject B" };

  const idA = await seedValidation(page, first);
  const idB = await seedValidation(page, second);
  expect(idA).not.toBe(idB);

  await test.step("the index lists both; opening each row opens its own instance pane", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: "First subject A" }).click();
    // Back to the index pane to open the second — the first row opened a pane to
    // the side, leaving the index visible.
    await page.locator(".wb-review-row", { hasText: "Second subject B" }).click();
  });

  await test.step("two bodies, each its own subject — nothing bleeds across", async () => {
    const bodyA = page.locator(".wb-review-body", { hasText: "First subject A" });
    const bodyB = page.locator(".wb-review-body", { hasText: "Second subject B" });
    await expect(bodyA).toBeVisible();
    await expect(bodyB).toBeVisible();
    // Each pane resolved *its* id: A's body does not carry B's subject.
    await expect(bodyA).not.toContainText("Second subject B");
    await expect(bodyB).not.toContainText("First subject A");
    // Each pane is bound to its own validation_id on the DOM.
    await expect(page.locator(`.wb-review-body[data-validation="${idA}"]`)).toHaveCount(1);
    await expect(page.locator(`.wb-review-body[data-validation="${idB}"]`)).toHaveCount(1);
  });

  await test.step("a reload brings both back live — the id on disk, no tombstone flash", async () => {
    // Only really persisted if these exact strings are on disk (`ui/src/panes.ts`).
    await persisted(page, `review#${idA}`);
    await persisted(page, `review#${idB}`);
    await page.reload();
    // Both restored panes resolve their still-held results — live bodies, and the
    // tombstone's "no longer loaded" never appears for a result the server holds.
    await expect(page.locator(`.wb-review-body[data-validation="${idA}"]`)).toBeVisible();
    await expect(page.locator(`.wb-review-body[data-validation="${idB}"]`)).toBeVisible();
    await expect(page.locator(".wb-review-tombstone")).toHaveCount(0);
  });

  // Put the arrangement back for whatever journey shares this workspace next.
  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});

/**
 * The toolchain gate (M6 staged review PR1), end to end in a real browser — and
 * the half no unit test can claim: **the captured log opens in the panel**.
 *
 * The gate runs in the checkout the subject session is actually writing in, so
 * this journey has to produce one: it starts an orchestrator, lets it spawn
 * workers through the real service (each really borrows a pool slot), and then
 * validates *a worker's own session id*. `WORKBENCH_GATE_FAKE=1` scripts the
 * exit codes and the output — nothing runs ruff or pytest inside a slot on CI —
 * but everything around them is production: the slot lookup, the git
 * fingerprint before and after, the bounded head+tail capture, the bounded
 * payload store, `GET /api/validation/payload/gate/{ref}`, and the expander.
 *
 * The last two steps matter as much as the first: a `high` result **awaits a
 * human**, and approving it leaves the bar quiet for whatever runs next.
 */
const SPAWN_PROMPT = "spawn workers please";

interface Roster {
  orchestrators: {
    orchestrator_id: string;
    workers: { worker_id: string; slot: string | null }[];
  }[];
}

/** Open Mission Control the way a user reaches a tool not on screen. */
async function openBoard(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">mission control");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Mission Control" }).first().click();
  await expect(page.locator(".wb-mission")).toBeVisible();
}

/** Drive the orchestrator the way a user does from the board: click its card to
 * open (and focus) its chat, then type into the pane that click just focused.
 * `.dv-active-group` scopes the input — an unscoped `.wb-chat-input textarea`
 * would be several elements once other panes are open (CLAUDE.md). */
async function sendToOrchestrator(page: Page, text: string): Promise<void> {
  await page.locator('.wb-mission-card[data-kind="orchestrator"] .wb-mission-title').click();
  const input = page.locator(".dv-active-group .wb-chat-input textarea");
  await input.fill(text);
  await input.press("Enter");
  await expect(
    page.locator(".dv-active-group .wb-msg-user").filter({ hasText: text }),
  ).toBeVisible();
}

test("a toolchain gate proves a worker's checkout, and its log opens in the panel", async ({
  page,
}) => {
  await openApp(page);

  await test.step("the pool is really there — or this journey tests a refusal", async () => {
    // Without a git repository every spawn is refused and the gate would answer
    // "no slot" for a reason that has nothing to do with the gate.
    const pool = (await (await page.request.get("/api/worktrees")).json()) as {
      problem: string | null;
    };
    expect(pool.problem, `the E2E workspace is not a git repository: ${String(pool.problem)}`).toBe(
      null,
    );
  });

  await test.step("an orchestrator spawns a worker, which borrows a slot", async () => {
    await openBoard(page);
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await quickbar.locator(".wb-qb-input").fill(">new orchestrator");
    await quickbar.locator(".wb-qb-row", { hasText: "New orchestrator session" }).first().click();
    await expect(page.locator('.wb-mission-card[data-kind="orchestrator"]')).toHaveCount(1);
    await sendToOrchestrator(page, SPAWN_PROMPT);
    await expect
      .poll(
        async () => {
          const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
          return roster.orchestrators.flatMap((crew) => crew.workers).length;
        },
        { timeout: 30_000 },
      )
      .toBeGreaterThan(0);
  });

  const workerId = await test.step("read the worker the gate will judge", async () => {
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    const worker = roster.orchestrators.flatMap((crew) => crew.workers)[0];
    expect(worker, "the orchestrator spawned no worker").toBeDefined();
    // A slot each is the whole reason the pool exists — and the thing the gate
    // resolves rather than being handed.
    expect(worker.slot).not.toBeNull();
    return worker.worker_id;
  });

  const validationId = await test.step("the gate runs in that worker's own checkout", async () => {
    // `checks: ["gates"]` — the toolchain gate alone. An empty `checks` runs
    // *every* registered check, and the reconciliation gate handed no spec would
    // add a `numeric` fail of its own, which is a different feature's evidence
    // in this journey's assertions.
    const id = await seedValidation(
      page,
      { kind: "session_output", ref: workerId, label: "Worker gates" },
      ["gates"],
    );
    expect(id).toMatch(/^val_/);
    return id;
  });

  await test.step("four gate lines, one per gate, and the failing one is a Fail", async () => {
    await openReviewPanel(page);
    await page.locator(".wb-review-row", { hasText: "Worker gates" }).click();
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body).toBeVisible();
    // One line per gate, not one grouped line — that is the design decision, and
    // it is visible here rather than only in a unit test.
    await expect(body.locator('.wb-evidence[data-kind="gate"]')).toHaveCount(4);
    await expect(body.locator('.wb-evidence[data-outcome="fail"]')).toHaveCount(1);
    await expect(body.locator(".wb-review-head .wb-pill")).toContainText("High risk");
  });

  await test.step("the payload route works: the captured log opens in the expander", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    const failing = body.locator('.wb-evidence[data-outcome="fail"]');
    // Lazily fetched: nothing is loaded until the expander is opened.
    await expect(failing.locator(".wb-evidence-log")).toHaveCount(0);
    await failing.locator("summary").click();
    const log = failing.locator(".wb-evidence-log");
    await expect(log).toBeVisible();
    // The scripted pytest failure, read back through
    // GET /api/validation/payload/gate/{ref} — the frame's dead handle, alive.
    await expect(log).toContainText("1 failed, 118 passed");
    await expect(log).toContainText("test_dispatch.py:118");
    await expect(failing.locator(".wb-evidence-argv")).toContainText("exit 1");
  });

  await test.step("a failing gate awaits a human, and approving leaves the bar quiet", async () => {
    const body = page.locator(`.wb-review-body[data-validation="${validationId}"]`);
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-approve").click();
    await expect(body.locator(".wb-review-approved")).toContainText("Approved by you");
  });

  await test.step("cleanup: the crew is reaped and its slots go back", async () => {
    // Through the REST stop path rather than the board's own button: this is
    // teardown, not a claim — a leaked lease is a checkout no later journey can
    // borrow for an hour, and `mission.spec.ts` is where reaping *from the
    // board* is the assertion. `REAP_PROMPT` is the agent-driven equivalent and
    // is exercised there too.
    const roster = (await (await page.request.get("/api/orchestrator")).json()) as Roster;
    for (const crew of roster.orchestrators) {
      await page.request.post(`/api/orchestrator/sessions/${crew.orchestrator_id}/stop`);
    }
    await expect
      .poll(
        async () => {
          const pool = (await (await page.request.get("/api/worktrees")).json()) as {
            slots: { state: string }[];
          };
          return pool.slots.filter((slot) => slot.state === "leased").length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);
  });

  await page.request.put("/api/layouts", { data: NO_LAYOUT });
});
