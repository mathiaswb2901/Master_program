/**
 * Journey — Mission Control: the fleet on one board, and an agent that runs it.
 *
 * The claim under test is the one nothing else in the app can make: **a blocked
 * agent in a session you never opened is visible and answerable**. So the order
 * of the steps is the argument. The journey opens the board, starts an
 * *orchestrator*, and lets that orchestrator spawn two workers — real sessions,
 * in real borrowed git worktrees, through the real service (`WORKBENCH_FAKE_AGENT`
 * replaces the model, never the mechanism). Nothing here ever opens a worker's
 * chat: every assertion is made on the board, and a worker's permission is
 * answered from the board, for a session this window holds no socket for.
 *
 * What each step proves, and why it could not be proven anywhere cheaper:
 *
 * - **an idle fleet** is a designed state, not an empty panel;
 * - **two workers appear** with a *slot each* — the whole reason the worktree
 *   pool exists, and something a unit test can only assert about ids;
 * - **each is blocked**, and its `Bash` command is on screen unredacted,
 *   because an approval you cannot read is one you cannot give informedly;
 * - **Deny lands**, over `POST /api/agents/sessions/{id}/permission`, with no
 *   agent socket for that session anywhere in this page;
 * - **stopping the crew reaps them** and gives the slots back — asserted
 *   against `GET /api/worktrees`, because a lease leaked here is a checkout
 *   nobody can use for an hour.
 *
 * The four-worker rendering, a spawn refused by a ceiling and the stale-card
 * path are in `MissionControl.test.tsx` and `test_orchestrator.py`, where they
 * can be staged in a millisecond rather than raced.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import { openApp, sendChat } from "./app";

/** The fake orchestrator's triggers (`services/fake_agent.py`). Each really
 * calls the real service — a refusal would be reported as a refusal. */
const SPAWN_PROMPT = "spawn workers please";
const REAP_PROMPT = "reap workers please";

/** Open the board the way a user reaches a tool that is not on screen. */
async function openBoard(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">mission control");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Mission Control" }).first().click();
  await expect(page.locator(".wb-mission")).toBeVisible();
}

/** Every card on the board. Scoped to the board, never a bare class selector —
 * an unscoped locator would pass against a broken plural app (CLAUDE.md). */
function cards(page: Page): Locator {
  return page.locator(".wb-mission .wb-mission-card");
}

/** Cards that are workers, which is what a crew is made of. */
function workerCards(page: Page): Locator {
  return page.locator('.wb-mission .wb-mission-card[data-kind="worker"]');
}

/** Leave the workspace with the arrangement every other journey expects. The
 * board is opened on demand, so it changes the (autosaved, per-workspace)
 * window — same guard, same reason, as the activity and layout journeys. */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("mission control: a crew, on one board", async ({ page }) => {
  await openApp(page);
  await openBoard(page);

  await test.step("the pool is really there — or this journey is testing a refusal", async () => {
    // Preflight, and it is worth an assertion of its own: without a git
    // repository the pool refuses every spawn, and every step below would fail
    // for a reason that has nothing to do with the board. `e2e/workspace.ts`
    // seeds the repository; this is what says so when the seed did not run.
    const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
    const pool = await (await context.get("/api/worktrees")).json();
    expect(pool.problem, `the E2E workspace is not a git repository: ${String(pool.problem)}`).toBe(
      null,
    );
    await context.dispose();
  });

  await test.step("the board already shows the fleet, and nothing is waiting", async () => {
    // This journey does **not** assert the empty state, and that is deliberate
    // rather than an omission: the suite runs serially against one backend and
    // several journeys before this one leave sessions behind, so an idle fleet
    // is not a state this position in the run can honestly reach. It is
    // asserted where it can be staged in a millisecond
    // (`MissionControl.test.tsx`, at zero, one and four cards).
    await expect(cards(page)).not.toHaveCount(0);
    await expect(workerCards(page)).toHaveCount(0);
    // §6.7: a quiet bar means nothing needs you — true here because every
    // earlier journey answered the prompts it raised.
    await expect(page.getByTestId("mission-status")).toHaveCount(0);
  });

  await test.step("an orchestrator starts, and appears as one", async () => {
    // Through the registered command, which is also the route a user has when
    // the board is not empty (the empty state's button is the other one).
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await quickbar.locator(".wb-qb-input").fill(">new orchestrator");
    await quickbar.locator(".wb-qb-row", { hasText: "New orchestrator session" }).first().click();
    // Its own card, labelled — a chat and an orchestrator are not the same
    // thing and the board says which is which.
    await expect(
      page.locator('.wb-mission .wb-mission-card[data-kind="orchestrator"]'),
    ).toHaveCount(1);
  });

  await test.step("it spawns two workers, each in a worktree of its own", async () => {
    await sendChat(page, SPAWN_PROMPT);
    await expect(workerCards(page)).toHaveCount(2, { timeout: 30_000 });

    // A slot each. Two workers in one checkout is exactly the failure the pool
    // exists to prevent, so this reads the *rendered* slot names rather than
    // trusting that two ids differ.
    const slots = await page
      .locator('.wb-mission .wb-mission-card[data-kind="worker"] .wb-mission-figures dd')
      .allInnerTexts();
    const named = slots.filter((text) => text.startsWith("slot-"));
    expect(named).toHaveLength(2);
    expect(new Set(named).size).toBe(2);
  });

  await test.step("each worker is blocked, and the board says on what", async () => {
    const prompts = workerCards(page).locator(".wb-mission-prompt");
    await expect(prompts).toHaveCount(2, { timeout: 30_000 });
    // Not redacted: the command an agent wants to run is the whole of what a
    // person is being asked to approve.
    await expect(prompts.first()).toContainText("Bash");
    await expect(prompts.first()).toContainText("scripted-permission");
    // A blocked card wears the attention state *and* sorts to the top — colour
    // is never the only signal (DESIGN.md §7).
    await expect(cards(page).first()).toHaveClass(/is-blocked/);
    // And the status bar carries the one number nothing else in the window can.
    await expect(page.getByTestId("mission-status")).toContainText("2");
  });

  await test.step("a permission is answered from the board, with no chat open for it", async () => {
    // No agent socket has *ever* been opened for either worker — that is the
    // claim, so it is checked against the two ids rather than a socket count
    // (earlier journeys opened plenty of their own).
    const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
    const roster = await (await context.get("/api/orchestrator")).json();
    const ids: string[] = roster.orchestrators.flatMap((crew: { workers: { worker_id: string }[] }) =>
      crew.workers.map((worker) => worker.worker_id),
    );
    await context.dispose();
    expect(ids).toHaveLength(2);
    const opened = await page.evaluate(
      (workerIds: string[]) =>
        performance
          .getEntriesByType("resource")
          .map((entry) => entry.name)
          .filter((name) => workerIds.some((id) => name.includes(`/ws/agent/${id}`))),
      ids,
    );
    expect(opened, "a worker's chat is never opened — the board answers for it").toEqual([]);

    const first = workerCards(page).locator(".wb-mission-prompt").first();
    await first.getByRole("button", { name: "Deny" }).click();
    // The chip goes, and the other worker's stays: this answered one prompt,
    // not the fleet.
    await expect(workerCards(page).locator(".wb-mission-prompt")).toHaveCount(1);
    await expect(page.getByTestId("mission-status")).toContainText("1");
  });

  await test.step("stopping the crew reaps the workers and returns their slots", async () => {
    const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
    const before = await (await context.get("/api/worktrees")).json();
    expect(before.slots.filter((slot: { state: string }) => slot.state === "leased")).toHaveLength(
      2,
    );

    await page.locator(".wb-mission-stop").first().click();

    // A worker that outlived its orchestrator would be an agent nobody is
    // watching, holding a slot nobody will release for an hour.
    await expect
      .poll(
        async () => {
          const pool = await (await context.get("/api/worktrees")).json();
          return pool.slots.filter((slot: { state: string }) => slot.state === "leased").length;
        },
        { timeout: 30_000 },
      )
      .toBe(0);

    const roster = await (await context.get("/api/orchestrator")).json();
    const workers = roster.orchestrators.flatMap(
      (crew: { workers: { outcome: string | null }[] }) => crew.workers,
    );
    expect(workers).toHaveLength(2);
    expect(workers.every((worker: { outcome: string | null }) => worker.outcome === "stopped")).toBe(
      true,
    );
    await context.dispose();

    // Nothing is left waiting on the user, so the reading hides again (§6.7).
    await expect(page.getByTestId("mission-status")).toHaveCount(0);
    // And the orchestrator's own tool path agrees the crew is gone.
    await sendChat(page, REAP_PROMPT);
    await expect(page.locator(".wb-chat")).toContainText("reaped 0 worker(s)", {
      timeout: 30_000,
    });
  });
});
