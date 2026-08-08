/**
 * Journey — validation surfaces in the app (M6 PR3).
 *
 * The order is the feature. First the **quiet** bar: with nothing validated, the
 * status reading is absent (§6.7 — a quiet bar means nothing needs you). Then a
 * `ValidationResult` is driven into existence the way the server really mints
 * one — `POST /api/validation/run`. With **no check registered** (#82's honest
 * default on this base), that run is `blocked`: a validation that could not judge
 * anything, which is a real end-to-end result needing no reconciliation check.
 *
 * The Review panel then shows that result: the **blocked** risk badge, and an
 * evidence gallery that *says why it is empty* rather than showing blankness. A
 * `blocked` result is medium-or-worse, so it is **awaiting approval** — the one
 * mandatory human decision — and Approve records it and reflects the recorded
 * `ValidationApproval`.
 *
 * The POST runs inside the page so it rides the same-origin `/api` proxy and
 * carries the launch token (fetched from the token endpoint, which is exempt);
 * enforcement is on, so a tokenless POST would 401.
 */

import { expect, test, type Page } from "@playwright/test";

import { openApp } from "./app";

interface ValidationSubject {
  kind: "session_output" | "file" | "objective";
  ref: string;
  label: string;
}

/** Mint a real result through the server, from inside the page so the token
 * rides along. Returns its `validation_id`. */
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
    const result = (await res.json()) as { validation_id: string; risk: string };
    return result.validation_id;
  }, subject);
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

test("validation surfaces: the quiet bar, the blocked badge, the approval gate", async ({
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

  await test.step("a run with no registered check mints a blocked result", async () => {
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

  await test.step("the blocked badge and an evidence gallery that says why", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body).toBeVisible();
    // The badge is the blocked pill; the gallery is empty and says so; the
    // summary carries the *why* (§ the honest default — never a silent green).
    await expect(body.locator(".wb-pill")).toContainText("Blocked");
    await expect(body.locator(".wb-review-none")).toContainText("No evidence");
    await expect(body).toContainText("nothing was validated");
  });

  await test.step("a blocked result awaits a human, and Approve records the decision", async () => {
    const body = page.locator(".wb-review-body");
    await expect(body.locator(".wb-review-awaiting")).toContainText("Awaiting approval");
    await body.locator(".wb-review-note").fill("acknowledged: nothing to judge");
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
