/**
 * Journey 9b — a visual artifact, expanded into its own pane (M5 item 3, PR 4).
 *
 * Journey 9 proves the artifact draws natively inside its card. This proves the
 * other half of the request: lifting one artifact out of the card into a
 * full-screen pane for review, and back again, without ever leaving the closed
 * union that makes it safe.
 *
 * Asserts, in the order they would break:
 *  - the card's Expand affordance opens a pane rendering the **same** scene
 *    graph — every leaf kind, drawn by the same components, no second path;
 *  - **zero network requests** while the pane renders. A visual payload has no
 *    field to fetch and the pane adds no code that would; this is the safety
 *    property, and a real browser is the only place its absence is provable;
 *  - annotate mode works in the pane exactly as in the card, and a note pointed
 *    at a part *there* travels back with the plan's decision made on the card —
 *    one `PlanResponse`, not a second channel;
 *  - a reload restores the pane bound to the **same** artifact id, and — because
 *    plans render live and a restart forgets them — it says so in a named
 *    tombstone rather than a dead pane (product principle 4c).
 */

import { expect, test, type Page, type Request } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { newSession, openApp, sendChat, workspaceReady } from "./app";

/** Pane ids as the next launch will read them from disk. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Leave the workspace with no saved arrangement, so the artifact pane this
 * journey opens does not follow the journeys that share the workspace. */
test.afterAll(async () => {
  const { request } = await import("@playwright/test");
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("expand an artifact into its own pane, annotate it, and restore it", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  // Let the session-open write land before recording (see visual.spec.ts): a
  // dock change debounces a PUT /api/layouts, and recording across it would
  // catch the control plane and call it the artifact fetching something.
  await page
    .waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname === "/api/layouts",
      { timeout: 3_000 },
    )
    .catch(() => undefined);

  await sendChat(page, "visual please");
  const card = page.locator(".wb-plan-card");
  await expect(card.locator(".wb-vis")).toBeVisible();

  const pane = page.locator(".wb-artifact-pane");

  await test.step("Expand opens a pane, and no request is issued to render it", async () => {
    const requests: string[] = [];
    const record = (request: Request): void => {
      requests.push(`${request.method()} ${request.url()}`);
    };
    page.on("request", record);

    await card.getByRole("button", { name: "Expand" }).click();
    await expect(pane).toBeVisible();
    await expect(pane.locator(".wb-vis")).toBeVisible();
    page.off("request", record);

    expect(requests, `unexpected requests: ${requests.join(", ")}`).toEqual([]);
  });

  await test.step("the pane draws every leaf kind through the same renderer", async () => {
    await expect(pane.locator(".wb-vis-metrics")).toHaveCount(1);
    await expect(pane.locator("svg.wb-vis-chart")).toHaveCount(1);
    await expect(pane.locator("table.wb-vis-table")).toHaveCount(2);
    await expect(pane.locator("svg.wb-vis-diagram")).toHaveCount(1);
    await expect(pane.locator(".wb-vis-diff")).toHaveCount(1);
    // The tab is closable chrome (opened on demand), titled by the artifact.
    await expect(page.locator(".wb-panel-tab", { hasText: "Day-ahead result" })).toBeVisible();
  });

  let artifactPane = "";
  await test.step("the pane's binding is a stable id, persisted to disk", async () => {
    await expect
      .poll(async () => (await persistedPaneIds(page)).filter((id) => id.startsWith("visual#")).length, {
        timeout: 10_000,
      })
      .toBe(1);
    artifactPane = (await persistedPaneIds(page)).find((id) => id.startsWith("visual#")) ?? "";
    // planId:nodeId — the node is the fake plan's `scene`.
    expect(artifactPane, "a pane bound to an artifact").toMatch(/^visual#[0-9a-f]+:scene$/);
  });

  await test.step("a note made in the pane travels back with the card's decision", async () => {
    await pane.getByRole("button", { name: "Annotate", exact: true }).click();
    // Pick a real part — a cell of the Before table — from the pane, not the card.
    await pane.locator('button[aria-label^="Annotate Before"]').first().click();
    const note = pane.locator("textarea.wb-plan-note-text");
    await expect(note).toBeVisible();
    await note.fill("second 02:00 uses the wrong fold");

    // The decision is still the card's; the note is the pane's. One PlanResponse.
    await card.getByRole("button", { name: "Approve" }).click();
    await expect(card.locator(".wb-plan-verdict")).toHaveText("Approved");
    const echo = page.locator(".wb-msg-block").last();
    await expect(echo).toContainText("plan approve");
    await expect(echo).toContainText("scene/leaf/2/row/0");
    await expect(echo).toContainText("second 02:00 uses the wrong fold");
  });

  await test.step("a reload restores the pane bound to the same artifact", async () => {
    const before = await persistedPaneIds(page);
    expect(before).toContain(artifactPane);
    await page.reload();
    await workspaceReady(page);

    // Same id, not "an artifact pane came back" but *that* one.
    await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(artifactPane);
    // Plans render live; a restart forgets them, so the restored pane is a named
    // tombstone with its one recovery — never a dead pane.
    const tombstone = page.locator(".wb-pane-note", {
      hasText: "This artifact is no longer loaded",
    });
    await expect(tombstone).toBeVisible();
    await expect(tombstone.getByRole("button", { name: "Close" })).toBeVisible();
  });
});
