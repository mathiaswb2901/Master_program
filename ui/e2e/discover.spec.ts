/**
 * Journey 12 — discoverability: the app tells you what it can do.
 *
 * The change request is one sentence from the owner after a day of features
 * landing — *"I am not sure how to use these features."* Everything below is
 * that sentence turned into assertions a build can fail on:
 *
 *  - a window nobody has arranged **opens with a welcome**, in a tab, not a
 *    modal — and its affordances are real: clicking "Split this pane in two"
 *    splits the pane, exactly as the chord beside it would;
 *  - the welcome **stays gone**: dismissal is workspace state
 *    (`.workbench/welcome.json`), so a reload does not bring the scaffolding
 *    back;
 *  - `Alt+K` opens the **keyboard reference**, which is *generated from the
 *    registry* — so the assertion here is not that some hand-written page
 *    exists, but that searching it finds a command by its chord, and that the
 *    chord it shows is the one that actually runs. The journey reads `Alt+S`
 *    off the screen and then presses it.
 *
 * The suite's workspace is seeded **dismissed** (`e2e/workspace.ts`), because
 * every other journey runs against a window that has been used before. This one
 * clears that flag through the app's own API to get first run back, and puts it
 * as it found it afterwards — so it holds whatever order the suite runs in.
 */

import { expect, request, test, type Page, type Route } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { launchSettled, openApp, workspaceReady } from "./app";
import { WELCOME_FILE } from "./workspace";

const DEFAULT_PANELS = ["Agent", "Editor", "Files", "Terminal"];

const welcome = (page: Page) => page.getByRole("region", { name: "Welcome to Workbench" });
const reference = (page: Page) => page.locator(".wb-keys");
const search = (page: Page) => page.getByRole("searchbox", { name: "Search shortcuts" });

async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** Pane ids as the next launch will read them back off disk. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Write the welcome file's bytes, the way the app writes them — through the
 * files API, which is the whole of its persistence (`panels/Keyboard.tsx`). */
async function writeWelcome(page: Page, content: string): Promise<void> {
  const response = await page.request.put("/api/files/content", {
    data: { path: WELCOME_FILE, content },
  });
  expect(response.ok(), "the welcome file was written").toBe(true);
}

async function setDismissed(page: Page, dismissed: boolean): Promise<void> {
  await writeWelcome(page, `${JSON.stringify({ dismissed }, null, 2)}\n`);
}

/** No saved arrangement, so nothing restores over an auto-opened panel — the
 * half of the auto-open rule that is *not* about the welcome file. */
async function clearLayouts(page: Page): Promise<void> {
  await page.request.put("/api/layouts", {
    data: { current: null, current_name: null, saved: [] },
  });
}

/** Run a QuickBar command by its row title — the keyboard path to anything. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

async function relaunch(page: Page): Promise<void> {
  await page.reload();
  await workspaceReady(page);
  await launchSettled(page);
}

/** A window nobody has arranged: no saved layout, no dismissal. */
async function firstRun(page: Page): Promise<void> {
  await clearLayouts(page);
  await setDismissed(page, false);
  await relaunch(page);
}

/**
 * Leave the workspace as the rest of the suite expects it: used before, and
 * arranged the way it always was. Not belt-and-braces — journeys after this one
 * count panel tabs, and a Keyboard tab left behind would fail them.
 */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.put("/api/files/content", {
    data: { path: WELCOME_FILE, content: `${JSON.stringify({ dismissed: true }, null, 2)}\n` },
  });
  await context.dispose();
});

test("a new window says what it is, and the reference teaches the chords", async ({ page }) => {
  await openApp(page);

  await test.step("a used window shows no scaffolding", async () => {
    // The state every other journey runs in, asserted rather than assumed: this
    // is what "never sees it again" has to mean a month later. Deliberately not
    // an assertion about the *arrangement* — this journey shares its workspace
    // with the ones before it, and whether they left four panels or five is
    // their business, not discoverability's.
    await expect(welcome(page)).toBeHidden();
    await expect(reference(page)).toBeHidden();
  });

  await test.step("a window nobody has arranged opens with the welcome", async () => {
    await firstRun(page);
    await expect(welcome(page)).toBeVisible();
    // A tab, not a modal: the window is complete and the welcome is one of its
    // panes. Nothing has to be dismissed before the app can be used.
    expect(await panels(page)).toEqual([...DEFAULT_PANELS, "Keyboard"].sort());
    await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
    // Four things to try, each carrying the chord that does the same thing.
    await expect(welcome(page).locator(".wb-welcome-action")).toHaveCount(4);
    await expect(
      welcome(page).locator(".wb-welcome-action", { hasText: "Split this pane in two" }),
    ).toContainText("Alt");
  });

  await test.step("and every one of them is reachable by keyboard alone", async () => {
    // A discovery surface for a keyboard-first app that can only be *used* with
    // a mouse teaches the wrong thing about the product it is introducing
    // (DESIGN.md §7). The card sits above the search box in DOM order, so
    // Shift+Tab from the box walks it backwards — "Got it" first, then the four
    // actions from the bottom up. That the walk arrives at all is the assertion:
    // it proves tab order follows reading order and that nothing in the card is
    // a div pretending to be a button.
    const split = welcome(page).locator(".wb-welcome-action", {
      hasText: "Split this pane in two",
    });
    await search(page).press("Shift+Tab");
    await expect(welcome(page).getByRole("button", { name: "Got it" })).toBeFocused();
    for (let step = 0; step < 3; step += 1) await page.keyboard.press("Shift+Tab");
    await expect(split).toBeFocused();
    // Focused *visibly*: the global `:focus-visible` ring is the one selection
    // signal this panel relies on (`styles/keyboard.css` says so in a comment),
    // and a comment is not a test.
    const outline = await split.evaluate((node) => {
      const style = getComputedStyle(node);
      return `${style.outlineStyle} ${style.outlineWidth}`;
    });
    expect(outline, "the focused welcome action has no visible ring").toBe("solid 2px");

    // Enter runs it — the same picker the mouse opens below.
    await page.keyboard.press("Enter");
    const picker = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(picker).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(picker).toBeHidden();
    await expect(welcome(page)).toBeHidden();
  });

  await test.step("its affordances do the thing, not describe it", async () => {
    // Back to a first-run window: the keyboard pass above used the card up, and
    // using it is what dismisses it.
    await firstRun(page);
    await welcome(page)
      .locator(".wb-welcome-action", { hasText: "Split this pane in two" })
      .click();
    // The real pane picker, on the QuickBar's own surface — byte for byte what
    // Alt+S opens (DESIGN.md §6.11).
    const picker = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(picker).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(picker).toBeHidden();
  });

  await test.step("and it is gone for good — through a reload", async () => {
    // Using it dismissed it: a user who has started does not need to be told
    // how to start.
    await expect(welcome(page)).toBeHidden();
    await relaunch(page);
    // The scaffolding is what must not come back — this is what "a user who has
    // used it for a month never sees it again" has to mean. Whether the *panel*
    // is still on screen is the layout system's business and deliberately not
    // asserted: it was open when the arrangement was saved, so a restore
    // bringing it back is correct, and the debounced autosave makes which of
    // the two happened a matter of milliseconds.
    await expect(welcome(page)).toBeHidden();
  });

  await test.step("Alt+K opens the reference from anywhere", async () => {
    // Back to the plain window first, so the chord is unambiguously what put
    // the panel there. Then pressed from inside the terminal, which is the
    // pass-through rule the panel itself explains: Alt is what reaches
    // Workbench from a surface that owns its own keyboard.
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(reference(page)).toBeHidden();
    await page.locator(".wb-terminal:not(.is-hidden) .xterm-screen").click();
    await page.keyboard.press("Alt+K");
    await expect(reference(page)).toBeVisible();
  });

  await test.step("every capability is in it, grouped by the tool that owns it", async () => {
    for (const group of ["Window", "Editor", "Agent", "Terminal", "Panes", "Layouts"]) {
      await expect(reference(page).locator(".wb-keys-group-title", { hasText: group })).toBeVisible();
    }
    // Generated, not written down: the user's own `shortcuts.md` entries are
    // commands too, so they are here as well.
    await expect(
      reference(page).locator(".wb-keys-row", { hasText: "Show the marker" }),
    ).toBeVisible();
    // And the reason a keymap has Alt twins at all, in plain words.
    await expect(
      page.getByRole("region", { name: "Why some chords reach the terminal" }),
    ).toBeVisible();
  });

  await test.step("a chord that would do nothing right now says so", async () => {
    // `Alt+A` annotates a plan the agent is waiting on an answer for, and no
    // session in this window is holding one — so the command is registered,
    // listed, and inert. `resolveCommand` drops a command whose `when()` is
    // false without so much as a `preventDefault`, so a row that read like every
    // other one would be teaching a reflex that silently fails: worse than
    // teaching nothing (§6.12). Chosen because its gate is a *transient window
    // condition* this journey controls (there is no pending plan on a freshly
    // relaunched window) rather than an ambient count of what earlier journeys
    // left behind: `Alt+9` — "jump to the ninth session" — read as unavailable
    // only while fewer than nine sessions existed, and a suite that grew past
    // that made a passing assertion fail with nothing about this feature having
    // changed. A test may not depend on how many journeys ran first.
    await search(page).fill("annotate the plan");
    const inert = reference(page).locator(".wb-keys-row");
    await expect(inert).toHaveCount(1);
    await expect(inert).toHaveClass(/is-unavailable/);
    await expect(inert).toContainText("not available yet");
    // Still listed, still carrying its chord: the reference teaches what exists.
    await expect(inert.locator(".wb-keycap")).toHaveText(["Alt", "A"]);

    // And a command that is live right now carries no such mark.
    await search(page).fill("new terminal");
    await expect(
      reference(page).locator(".wb-keys-row", { hasText: "New terminal" }).first(),
    ).not.toHaveClass(/is-unavailable/);
    await search(page).fill("");
  });

  let discovered = "";
  await test.step("searching it finds a command, and states its chord", async () => {
    await search(page).fill("split");
    const rows = reference(page).locator(".wb-keys-row");
    await expect(rows).toHaveCount(2);
    const row = reference(page).locator(".wb-keys-row", { hasText: "Split this pane to the right" });
    discovered = (await row.locator(".wb-keycap").allTextContents()).join("+");
    expect(discovered, "the split row renders its chord as keycaps").toBe("Alt+S");

    // A chord is also how you search for one — the question this surface is
    // usually opened with is "what was that key?"
    await search(page).fill("alt+m");
    await expect(reference(page).locator(".wb-keys-row")).toHaveCount(1);
    await expect(reference(page).locator(".wb-keys-row")).toContainText("focus mode");
  });

  await test.step("and the chord it taught actually does the thing", async () => {
    await search(page).fill("");
    await page.locator(".wb-panel-tab", { hasText: "Files" }).click();
    await page.keyboard.press(discovered);
    const picker = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(picker).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(picker).toBeHidden();
  });

  await test.step("the status bar keeps one permanent way back to it", async () => {
    await page.locator(".wb-panel-tab", { hasText: "Keyboard" }).getByRole("button").click();
    await expect(reference(page)).toBeHidden();
    const chip = page.getByRole("button", { name: "Keys" });
    // The mouse path teaches the keyboard one: the tooltip names the chord, and
    // it is read from the registry rather than written into the button.
    await expect(chip).toHaveAttribute("title", "Keyboard shortcuts — Alt+K");
    await chip.click();
    await expect(reference(page)).toBeVisible();
  });

  await test.step("a welcome that cannot be read counts as dismissed, not as new", async () => {
    // The failure direction that never nags (§6.12), and the one a single
    // `catch` gets wrong. Both halves below are a workspace somebody has used
    // for a month, and in both the *only* honest reading is "this window has
    // been here before" — a 404 is the one answer that means first run.
    //
    // Every launch here is against a cleared arrangement, because that is the
    // other half of the auto-open rule: with a layout on disk nothing opens
    // anyway and the assertion would pass without proving a thing.

    // Truncated mid-write, hand-edited, or written by a schema that has moved
    // on: present, and not parseable.
    await writeWelcome(page, '{"dismissed": fal');
    await clearLayouts(page);
    await relaunch(page);
    await expect(welcome(page)).toBeHidden();
    expect(await panels(page)).toEqual(DEFAULT_PANELS);

    // And the transient case: the file says the welcome is *due*, but the one
    // GET that would have read it fails for a reason that is not "no such
    // file". A backend hiccup during launch must not reopen the scaffolding.
    await writeWelcome(page, `${JSON.stringify({ dismissed: false }, null, 2)}\n`);
    const welcomeRead = (url: URL): boolean =>
      url.pathname === "/api/files/content" && url.searchParams.get("path") === WELCOME_FILE;
    const fail500 = (route: Route): Promise<void> =>
      route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"disk"}' });
    await page.route(welcomeRead, fail500);
    await clearLayouts(page);
    await relaunch(page);
    await expect(welcome(page)).toBeHidden();
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await page.unroute(welcomeRead, fail500);

    await setDismissed(page, true);
  });

  await test.step("reset leaves the workspace as the next journey expects it", async () => {
    // Deliberately the *last* thing that touches disk, and deliberately a poll:
    // every step above ends by reloading a window whose arrangement is saved on
    // a debounce, so this is where the journey waits for the writing to stop.
    // A step added after this one has to re-establish the same thing or the
    // next journey inherits whichever write happened to land last.
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect
      .poll(() => persistedPaneIds(page), { timeout: 10_000 })
      .toEqual(["agent", "editors", "files", "terminal"]);
  });

  await test.step("a window that has been arranged is never interrupted", async () => {
    // The other half of the auto-open rule, and the half that makes it
    // deterministic rather than a race with the layout restore (§6.12): there
    // is an arrangement on disk now, so it is the truth about which panels are
    // open — even with the welcome un-dismissed, nothing opens over it.
    await setDismissed(page, false);
    await relaunch(page);
    await expect(welcome(page)).toBeHidden();
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await setDismissed(page, true);
  });
});
