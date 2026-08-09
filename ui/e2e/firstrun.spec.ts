/**
 * Journey 13 — first run / Setup: the app greets a fresh launch, says honestly
 * what is connected, and then gets out of the way (M7 §2).
 *
 * The welcome card (journey 12) teaches *the window*; Setup teaches *the
 * connections*. Everything below is the ROADMAP's first-run exit criterion
 * turned into assertions a headless build can fail on:
 *
 *  - a window nobody has arranged **opens the Setup walkthrough**, a tab beside
 *    the welcome card — not a modal; nothing has to be dismissed to use the app;
 *  - each check renders its **honest state**: under the fake agent Claude is
 *    signed out, so its row is *action needed* and carries the exact command
 *    `claude /login` as copyable text — never a button that logs in;
 *  - it **teaches** by pointing at the window basics, not duplicating them;
 *  - **dismissal is workspace state** (`.workbench/setup.json`): "Got it" retires
 *    the tab and a relaunch does not bring it back — it never nags again;
 *  - a window that **already has `.workbench` state** (the state every other
 *    journey runs in) shows no walkthrough at all;
 *  - and, in the two tests at the bottom, that first run is the **same window
 *    every time**: both teaching tabs greet, and the same one is in front. That
 *    is the half of the adoption gate a demo cannot check for you, because a
 *    rehearsed launch is not the launch a stranger gets.
 *
 * The workspace is seeded **dismissed** (`e2e/workspace.ts`); this journey clears
 * that through the app's own API to get first run back, and puts it as it found
 * it, so it holds whatever order the suite runs in — the discover journey's
 * convention exactly. It clears `welcome.json` too, so `afterAll` restores both.
 */

import { expect, request, test, type Page } from "@playwright/test";

import { launchSettled, openApp, workspaceReady } from "./app";
import { SETUP_FILE, WELCOME_FILE } from "./workspace";

const setup = (page: Page) => page.getByRole("region", { name: "Set up Workbench" });
const welcome = (page: Page) => page.getByRole("region", { name: "Welcome to Workbench" });

/**
 * A dock tab, by the title a human reads.
 *
 * Anchored on the tab's own accessible control — every closable panel renders a
 * `Close <title>` button — rather than on tab text, which dockview renders as a
 * bare text node with the close glyph run into it. The dockview class is only
 * here for the one thing the accessibility tree does not say: which tab is the
 * active one.
 */
const tab = (page: Page, title: string) =>
  page.locator(`.dv-tab:has([aria-label="Close ${title}"])`);

/**
 * Take the app's layout persistence off the wire for the whole journey.
 *
 * This journey's subject is *the absence of a saved arrangement*, and that
 * precondition used to be raced rather than held. The layout system debounces
 * every layout change into `PUT /api/layouts` 500 ms later (`Layouts.tsx`), and
 * a page that is still alive when the journey clears the file can land its
 * pending write **after** the clear — putting a non-null `current` back on disk,
 * which is exactly the condition the auto-open declines to greet. The helper
 * below used to try to out-wait that with an extra reload; a wait is not a
 * guarantee, and it failed twice in one day's pipelines.
 *
 * So the writer is removed from the equation instead: the app's own layout
 * writes are answered here, with the success it would have got, and never reach
 * disk. Nothing in this journey asserts anything about layout persistence — it
 * asserts what a window with *no* arrangement does — so the only writer left is
 * the journey itself, and "no saved arrangement" becomes a fact rather than a
 * hope. Out-of-band `page.request` calls do not go through page routing, which
 * is what lets `clearLayouts` below still land.
 */
async function holdLayoutWrites(page: Page): Promise<void> {
  await page.route("**/api/layouts", async (route) => {
    if (route.request().method() !== "PUT") return route.fallback();
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ok: true }),
    });
  });
}

async function writeDismissal(page: Page, path: string, dismissed: boolean): Promise<void> {
  const response = await page.request.put("/api/files/content", {
    data: { path, content: `${JSON.stringify({ dismissed }, null, 2)}\n` },
  });
  expect(response.ok(), `${path} was written`).toBe(true);
}

const writeSetup = (page: Page, dismissed: boolean): Promise<void> =>
  writeDismissal(page, SETUP_FILE, dismissed);

async function readSetupFile(page: Page): Promise<{ dismissed?: unknown }> {
  const response = await page.request.get(
    `/api/files/content?path=${encodeURIComponent(SETUP_FILE)}`,
  );
  expect(response.ok(), "the setup file is readable").toBe(true);
  const body = (await response.json()) as { content: string };
  return JSON.parse(body.content) as { dismissed?: unknown };
}

/** No saved arrangement, so nothing restores over the auto-opened panel — the
 * half of the auto-open rule that is not about the setup file.
 *
 * Read back, not fired and forgotten: with `holdLayoutWrites` in force this is
 * the only writer, so a `current` that is not null here means the interception
 * is not working and the journey would otherwise fail several steps later with
 * a symptom that names none of this. */
async function clearLayouts(page: Page): Promise<void> {
  await page.request.put("/api/layouts", {
    data: { current: null, current_name: null, saved: [] },
  });
  const response = await page.request.get("/api/layouts");
  const body = (await response.json()) as { state: { current: unknown } };
  expect(body.state.current, "the arrangement really is cleared").toBeNull();
}

async function relaunch(page: Page): Promise<void> {
  await page.reload();
  await workspaceReady(page);
  await launchSettled(page);
}

/**
 * A window nobody has arranged and that has not answered Setup.
 *
 * One reload, because there is nothing left to out-wait: `holdLayoutWrites` has
 * already taken the app's debounced writer off the wire, so the arrangement this
 * clears stays cleared and the page that comes back reads the one state the
 * auto-open is about. (This used to reload twice, hoping a pending write would
 * settle in between — see `holdLayoutWrites` for what that cost.)
 */
async function firstRun(page: Page): Promise<void> {
  await writeSetup(page, false);
  await clearLayouts(page);
  await relaunch(page);
}

/** Put the workspace back the way the rest of the suite expects it: used before,
 * and unarranged. A Setup tab left behind would fail the panel-counting journeys,
 * and a welcome card left un-dismissed would open one in every journey after
 * this — this journey un-dismisses both. */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  for (const path of [SETUP_FILE, WELCOME_FILE]) {
    await context.put("/api/files/content", {
      data: { path, content: `${JSON.stringify({ dismissed: true }, null, 2)}\n` },
    });
  }
  await context.dispose();
});

test("a fresh launch greets you, says what is connected, and then gets out of the way", async ({
  page,
}) => {
  await holdLayoutWrites(page);
  await openApp(page);

  await test.step("a workspace that already has .workbench shows no walkthrough", async () => {
    // The state every other journey runs in, asserted rather than assumed. This
    // is what "never sees it again" has to mean on a used workspace.
    await expect(setup(page)).toBeHidden();
  });

  await test.step("a window nobody has arranged opens the Setup walkthrough", async () => {
    await firstRun(page);
    await expect(setup(page)).toBeVisible();
    // A tab, not a modal: the workspace tree is right there beside it.
    await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
    await expect(setup(page).getByRole("heading", { name: "Let's get you connected" })).toBeVisible();
  });

  await test.step("each check renders its honest state", async () => {
    // Every connection has a row. The ids are stable; the titles are what a
    // human reads.
    for (const title of ["Claude", "Office", "OnlyOffice", "Workspace"]) {
      await expect(
        setup(page).locator(".wb-setup-row-title", { hasText: new RegExp(`^${title}$`) }),
      ).toBeVisible();
    }
    // Under the fake agent there is no real Claude, so it is signed out —
    // action needed, with the exact command as copyable text, never a button.
    const claude = setup(page).locator(".wb-setup-row.is-action_needed", { hasText: "Claude" });
    await expect(claude).toBeVisible();
    await expect(claude.locator(".wb-setup-instruction")).toHaveText("claude /login");
    // And nowhere in the walkthrough is there a control that signs you in.
    await expect(setup(page).getByRole("button", { name: /sign in/i })).toHaveCount(0);
  });

  await test.step("the status bar carries the unanswered reading", async () => {
    // The quiet-bar chip, present while the walkthrough is unanswered and
    // something needs action; it retires with the walkthrough below.
    await expect(page.locator(".wb-setup-chip")).toBeVisible();
  });

  await test.step("it teaches the window without duplicating the welcome card", async () => {
    await expect(setup(page).getByText("Learn the window")).toBeVisible();
    await expect(
      setup(page).getByRole("button", { name: "Show the welcome card" }),
    ).toBeVisible();
    await expect(setup(page).getByRole("button", { name: "Keyboard shortcuts" })).toBeVisible();
  });

  await test.step("Got it retires the tab and persists the dismissal", async () => {
    await setup(page).getByRole("button", { name: "Got it" }).click();
    await expect(setup(page)).toBeHidden();
    // The chip retires with it.
    await expect(page.locator(".wb-setup-chip")).toBeHidden();
    // Dismissal is workspace state, written through the files API.
    expect((await readSetupFile(page)).dismissed).toBe(true);
  });

  await test.step("a relaunch does not bring it back — it never nags again", async () => {
    // Clear the arrangement first, so the *only* thing that could reopen the
    // walkthrough is the auto-open — and the persisted dismissal is what keeps
    // it shut. A saved arrangement still carrying the just-closed pane would
    // restore it by the ordinary layout mechanism, which is not what "never
    // nags" is about — that is the dismissal flag, asserted here.
    await clearLayouts(page);
    await relaunch(page);
    await expect(setup(page)).toBeHidden();
    await expect(page.locator(".wb-setup-chip")).toBeHidden();
  });
});

/**
 * One teaching tab wins, and it is always the same one (M7 §2's adoption gate).
 *
 * The two greetings — the welcome card (*the window*) and Setup (*the
 * connections*) — both open as centre tabs on a window nobody has arranged, and
 * they used to decide independently. Four identical launches of this scenario
 * produced three different windows: the welcome card in front, Setup in front,
 * and twice **no welcome card at all** — because opening a panel is a layout
 * change, the layout system debounces it to `layouts.json`, and the surface that
 * opened first flipped the "is this window arranged?" answer that the other one
 * was about to read. Whatever a demo rehearses is not what a stranger sees.
 *
 * So: both greetings are present, and Setup is the one in front. Ordering is
 * decided in `ui/src/firstRun.ts`, whose unit tests cover the mechanics; this is
 * the claim in the browser, where the race actually lived.
 */
test("one teaching tab wins, always the same one", async ({ page }) => {
  await openApp(page);
  await writeDismissal(page, SETUP_FILE, false);
  await writeDismissal(page, WELCOME_FILE, false);

  // Staged by *ending* the app rather than by muzzling it: `holdLayoutWrites`
  // would also suppress the thing under test here, because the collision this
  // journey guards against is one greeting's own arrangement write being read
  // by the other. On `about:blank` there is no app, so nothing can write over
  // the clear — and the launch that follows persists exactly as a real one does.
  await page.goto("about:blank");
  await clearLayouts(page);
  await page.goto("/");
  await workspaceReady(page);
  await launchSettled(page);

  // Both greet — neither is silently dropped by the other's side effects.
  await expect(tab(page, "Keyboard")).toBeVisible();
  await expect(tab(page, "Setup")).toBeVisible();

  // …and Setup is the tab the window lands on. Asserted on the tab's own active
  // state *and* on the body being on screen: a tab strip can carry a highlighted
  // tab whose panel lost the race to render.
  await expect(tab(page, "Setup")).toHaveClass(/dv-active-tab/);
  await expect(tab(page, "Keyboard")).not.toHaveClass(/dv-active-tab/);
  await expect(setup(page)).toBeVisible();
  await expect(setup(page).getByRole("heading", { name: "Let's get you connected" })).toBeVisible();
  // The welcome card is beside it — a tab away, not behind a modal, and not in
  // front of the surface that was supposed to win.
  await expect(welcome(page)).toBeHidden();
});

/**
 * …and it still wins when one greeting is slow to make up its mind.
 *
 * This is the same claim as above with the timing pinned instead of hoped for.
 * The collision needs one surface to answer more than half a second after the
 * other — which is what a loaded CI box supplies for free and a developer's
 * machine reliably does not — so the welcome card's dismissal read is held up
 * here on purpose. It is injected latency standing in for a slow machine, not a
 * widened timeout: the assertions below are unchanged and none of them waits
 * longer than it did.
 *
 * Against the behaviour this replaced, the delay is enough: Setup greets first,
 * its new pane is debounced into `layouts.json` 500 ms later, and the welcome
 * card — arriving after that — reads a window that "already has an arrangement"
 * and silently declines. A stranger's first launch then has one teaching tab
 * where it should have two, and nothing anywhere says so.
 */
test("a greeting that answers late still greets", async ({ page }) => {
  await openApp(page);
  await writeDismissal(page, SETUP_FILE, false);
  await writeDismissal(page, WELCOME_FILE, false);

  await page.route("**/api/files/content**", async (route) => {
    if (!route.request().url().includes("welcome.json")) return route.fallback();
    await new Promise((resolve) => setTimeout(resolve, 900));
    await route.continue();
  });

  await page.goto("about:blank");
  await clearLayouts(page);
  await page.goto("/");
  await workspaceReady(page);
  await launchSettled(page);

  await expect(tab(page, "Keyboard")).toBeVisible();
  await expect(tab(page, "Setup")).toBeVisible();
  await expect(tab(page, "Setup")).toHaveClass(/dv-active-tab/);
});
