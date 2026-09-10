/**
 * Journey — one session, two surfaces: the state a pane does not own.
 *
 * Both tests here are regressions for shipped bugs, and both are the same
 * mistake in two places: a pane rendering *its own copy* of something the window
 * knows once (CLAUDE.md, panes §1 — "nothing assumes it is the only one of
 * itself", §2 — "a pane is a view onto a resource it does not own").
 *
 *  1. **A permission answered somewhere else.** The prompt reaches the human on
 *     two channels — the chat pane's own `/ws/agent` socket and the fleet-wide
 *     `session_permission` frame Mission Control renders — so a card that
 *     remembered its own click kept asking a question that was already settled,
 *     and pressing Allow again reached a closed prompt (404 by design). Answered
 *     on the board, the card in the chat pane has to settle; answered in the
 *     chat, the chip on the board has to go.
 *
 *  2. **A Done dot in a pane of its own.** The done/error markers are cleared by
 *     *viewing* a session (DESIGN.md §2.6, "finished since last viewed"), and
 *     clearing was wired only into the session picker — so a session watched
 *     exclusively through its own pane, which is the whole point of the pane
 *     system, wore its Done dot for the rest of the window's life.
 *
 * Neither is reachable from a unit test: both are two views of one record
 * meeting inside a running window, and it is the *meeting* that was broken.
 *
 * Every locator is scoped to the pane it means. An unscoped `.wb-perm-card` or
 * `.wb-chat-header` would pass against exactly the broken plural app these tests
 * exist to catch.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import { newSession, openApp, sendChat } from "./app";

/** The fake agent's scripted prompt (`services/fake_agent.py`) and the command
 * it asks about — unredacted on both surfaces, deliberately. */
const ASK = "ask permission before continuing";
/** The same trigger a second time (it matches anywhere in the message), worded
 * differently so the two user rows in this one conversation stay tellable
 * apart. The session keeps the title its *first* message gave it, which is what
 * the board is scoped by below. */
const ASK_AGAIN = "ask permission once more, please";
/** The prompt the third test answers from a *second window* — the one path
 * where a card is closed without this window ever learning the verdict. */
const ASK_ELSEWHERE = "ask permission from the other window";
/** The prompt whose first board answer is refused by the server. */
const ASK_STALE = "ask permission, and lose the first answer";
/** The conversation two panes watch at once, and the prompt they both see.
 * Deliberately shares no prefix with any other journey's session title — two
 * matching picker rows is a strict-mode failure, not a bug. */
const SHARED = "one conversation, two panes watching it";
const ASK_BOTH = "ask permission where both panes can see it";
const COMMAND = "echo scripted-permission";

/** The board's REST door onto a prompt (`POST …/sessions/<id>/permission`) —
 * the request the stale-click journey intercepts. */
const ANSWER_ROUTE = "**/api/agents/sessions/*/permission";

/** A trigger that keeps a call in flight for three seconds, which is the window
 * a turn has to end *in* for the second test: the marker only exists for a
 * session that finished while you were looking somewhere else. */
const SLOW = "slow tool, and this pane is watching";

/** The Agent panel — the picker plus the chat for the session it is showing. */
const browser = (page: Page): Locator => page.locator(".wb-agent");

/** Open Mission Control the way a user reaches a tool that is not on screen. */
async function openBoard(page: Page): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-input").fill(">mission control");
  await quickbar.locator(".wb-qb-row", { hasText: "Show Mission Control" }).first().click();
  await expect(page.locator(".wb-mission")).toBeVisible();
}

/** The board's chips for one session, named by the title its first message gave
 * it — the suite shares a backend, so "the first card" is whichever session an
 * earlier journey left behind. */
function boardPrompts(page: Page, title: string): Locator {
  return page
    .locator(".wb-mission .wb-mission-card")
    .filter({ hasText: title })
    .locator(".wb-mission-prompt");
}

/**
 * Leave the workspace with the arrangement every other journey expects.
 *
 * Both tests persist window state (a board, a split), and the journeys after
 * this one assume the default four panes — so this runs after *each* test
 * rather than after the file, which also gives the second test a clean window
 * to build its split in.
 */
test.afterEach(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("a permission settles on every surface, whichever one answered it", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  const pane = browser(page);

  await test.step("a blocked agent puts a question in the chat", async () => {
    await sendChat(page, ASK);
    const card = pane.locator(".wb-perm-card");
    await expect(card).toContainText(COMMAND);
    await expect(card.getByRole("button", { name: "Allow" })).toBeVisible();
  });

  await test.step("and the same question on the board, for the same session", async () => {
    await openBoard(page);
    await expect(boardPrompts(page, ASK)).toHaveCount(1);
    await expect(boardPrompts(page, ASK)).toContainText(COMMAND);
  });

  await test.step("answering it there settles the card here", async () => {
    // The bug, exactly: before the fix the card kept its buttons under a
    // question the server had already closed, and pressing one of them hit a
    // settled prompt — a 404 the user saw as nothing happening at all.
    await boardPrompts(page, ASK).getByRole("button", { name: "Allow" }).click();
    const card = pane.locator(".wb-perm-card");
    await expect(card).toHaveClass(/is-decided/);
    await expect(card.locator(".wb-perm-decision")).toHaveText("Allowed");
    await expect(card.getByRole("button", { name: "Allow" })).toHaveCount(0);
    await expect(boardPrompts(page, ASK)).toHaveCount(0);
  });

  await test.step("and the other way round: answered here, the chip goes there", async () => {
    await expect(pane.locator(".wb-chat-header .wb-badge")).toContainText("Idle");
    await sendChat(page, ASK_AGAIN);
    await expect(boardPrompts(page, ASK)).toHaveCount(1);

    const card = pane.locator(".wb-perm-card").last();
    await card.getByRole("button", { name: "Deny" }).click();
    await expect(card.locator(".wb-perm-decision")).toHaveText("Denied");
    // The board is the *server's* view of what is still open, so this is the
    // round trip landing rather than an optimistic chip removal.
    await expect(boardPrompts(page, ASK)).toHaveCount(0);
    await expect(page.getByTestId("mission-status")).toHaveCount(0);
  });
});

/**
 * The third outcome, and the reason it exists.
 *
 * The frame that retracts a prompt carries no verdict — it says only that the
 * question is closed — so a window that did not answer it knows exactly that
 * much. Rendering "Allowed" here would claim an approval the agent may never
 * have received, and this is also the shape a **timeout** arrives in (the
 * server denies a prompt after ten minutes and publishes the same empty set);
 * a second window is the same path, reachable in five seconds.
 */
test("a prompt answered in another window stops asking here, and invents nothing", async ({
  page,
  context,
}) => {
  await openApp(page);
  await newSession(page);
  const card = browser(page).locator(".wb-perm-card");

  await sendChat(page, ASK_ELSEWHERE);
  await expect(card).toContainText(COMMAND);
  await expect(card.getByRole("button", { name: "Allow" })).toBeVisible();

  const other = await context.newPage();
  await test.step("a second window opens the same conversation and answers it", async () => {
    await openApp(other);
    await browser(other).locator(".wb-session-row", { hasText: ASK_ELSEWHERE }).click();
    const there = browser(other).locator(".wb-perm-card");
    await there.getByRole("button", { name: "Allow" }).click();
    await expect(there.locator(".wb-perm-decision")).toHaveText("Allowed");
  });

  await test.step("this window stops asking, and says only what it knows", async () => {
    await expect(card).toHaveClass(/is-decided/);
    await expect(card.locator(".wb-perm-decision")).toHaveText("No longer waiting");
    await expect(card.getByRole("button", { name: "Allow" })).toHaveCount(0);
  });
  await other.close();
});

/**
 * The click the server refuses, and the verdict that must not survive it.
 *
 * Answering from the board is a POST, so unlike the chat pane's own socket it is
 * *told* whether the click landed — and a stale click is told `404` by design
 * (`resolve_permission`): the prompt had already timed out, or been answered
 * somewhere else, before this one arrived. The shared record has no undo
 * (`settlePermission` keeps the first verdict), so a verdict written on the way
 * *into* that POST could never be taken back, and every card for the request sat
 * there reading "Allowed" for a decision the agent never received.
 *
 * The 404 is injected rather than raced: the real one needs a prompt to close in
 * the gap between two clicks, which is a coin toss, and a journey that only
 * sometimes reproduces the bug is not a regression test. Everything after the
 * intercept is the real server again — including the second answer, which proves
 * the honest "No longer waiting" is not a dead end.
 */
test("a board answer the server refuses claims nothing, and the real one still lands", async ({
  page,
}) => {
  await openApp(page);
  await newSession(page);
  const card = browser(page).locator(".wb-perm-card");

  await sendChat(page, ASK_STALE);
  await expect(card).toContainText(COMMAND);
  await openBoard(page);
  await expect(boardPrompts(page, ASK_STALE)).toHaveCount(1);

  await test.step("the answer comes home a 404 — that prompt is not open any more", async () => {
    await page.route(ANSWER_ROUTE, (route) =>
      route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ detail: "that request is no longer awaiting a decision" }),
      }),
    );
    await boardPrompts(page, ASK_STALE).getByRole("button", { name: "Allow" }).click();
  });

  await test.step("so the card says what this window knows, and not a word more", async () => {
    // The regression, exactly. The verdict used to be recorded before the POST,
    // and nothing could take it back afterwards.
    await expect(card).toHaveClass(/is-decided/);
    await expect(card.locator(".wb-perm-decision")).toHaveText("No longer waiting");
    await expect(card.getByRole("button", { name: "Allow" })).toHaveCount(0);
  });

  await test.step("and the board stops pretending the click landed", async () => {
    // The chip went optimistically; `refresh()` re-reads the server's own open
    // set, which still holds this prompt because the POST never reached it.
    await expect(boardPrompts(page, ASK_STALE)).toHaveCount(1);
  });

  await test.step("answered for real, the verdict fills the blank in", async () => {
    // "No longer waiting" is an absence of knowledge, not an answer, so the
    // window that goes on to *obtain* the verdict may still record it — without
    // this the one surface that made the decision is the one reading "closed,
    // and I was not told". It also leaves the fake agent unblocked, which the
    // journeys after this one depend on.
    await page.unroute(ANSWER_ROUTE);
    await boardPrompts(page, ASK_STALE).getByRole("button", { name: "Allow" }).click();
    await expect(card.locator(".wb-perm-decision")).toHaveText("Allowed");
    await expect(boardPrompts(page, ASK_STALE)).toHaveCount(0);
  });
});

/**
 * Two panes, one session — the plural shape this file was otherwise missing.
 *
 * The journeys above prove two *kinds* of surface agree (a card and the board)
 * and two *windows* agree. Neither is the case CLAUDE.md's panes clause actually
 * names: two instances of the same tool, mounted at once, pointed at one
 * resource. `revealPane`'s picker offers exactly that — the default `agent`
 * panel plus a dedicated `agent#<id>` pane on a live session — and "it works
 * with one is not evidence", so the complement of the independence test (state
 * that is genuinely shared stays shared) is asserted rather than reasoned about
 * from `permissions` being a global map.
 */
test("two panes on one session answer with one voice", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  const panel = browser(page);

  await test.step("the conversation earns a name the split picker can offer", async () => {
    await sendChat(page, SHARED);
    // The picker builds its rows once, when it opens, from the same `folders`
    // slice this row renders — so waiting for the row here is what stops the
    // dialog opening a beat before the title refresh lands.
    await expect(panel.locator(".wb-session-row", { hasText: SHARED })).toBeVisible();
  });

  await test.step("and a second pane is bound to that same session", async () => {
    await page.keyboard.press("Alt+S");
    const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(dialog).toBeVisible();
    // Not "New agent session" — an *existing live session* row, which is the
    // arrangement nothing else opens: one conversation, two panes.
    await dialog.locator(".wb-qb-row", { hasText: SHARED }).first().click();
    await expect(dialog).toBeHidden();
    await expect(page.locator(".wb-chat")).toHaveCount(2);
  });

  const own = page.locator(".dv-groupview", {
    has: page.locator(".wb-panel-tab", { hasText: SHARED }),
  });
  const here = own.locator(".wb-perm-card");
  const there = panel.locator(".wb-perm-card");

  await test.step("one prompt, and both panes are asking it", async () => {
    const input = own.locator(".wb-chat-input textarea");
    await input.fill(ASK_BOTH);
    await input.press("Enter");
    await expect(here).toContainText(COMMAND);
    await expect(there).toContainText(COMMAND);
    await expect(there.getByRole("button", { name: "Deny" })).toBeVisible();
  });

  await test.step("answering in one settles the other, with the same verdict", async () => {
    await here.getByRole("button", { name: "Deny" }).click();
    await expect(here.locator(".wb-perm-decision")).toHaveText("Denied");
    // "Denied", not "No longer waiting": this window *made* the decision, and
    // both panes read the one record rather than each remembering its own click.
    await expect(there).toHaveClass(/is-decided/);
    await expect(there.locator(".wb-perm-decision")).toHaveText("Denied");
    await expect(there.getByRole("button", { name: "Deny" })).toHaveCount(0);
  });

  await test.step("and the window goes back the way the next journey expects it", async () => {
    // Closed here rather than left to `afterEach`. That hook resets the *saved*
    // layout, but the dock's autosave is debounced and can land after the PUT
    // and put this pane straight back — which the next journey meets as two
    // chat boxes under one unscoped locator, several steps from the cause. A
    // pane this journey opened is a pane this journey closes.
    await page.locator(".wb-panel-tab", { hasText: SHARED }).click();
    await page.keyboard.press("Alt+X");
    await expect(page.locator(".wb-chat")).toHaveCount(1);
  });
});

test("a session watched only through its own pane lets go of its Done dot", async ({ page }) => {
  await openApp(page);
  await newSession(page);
  const pane = browser(page);
  const FIRST = "the conversation in the panel";

  await test.step("one session in the panel, one in a pane of its own", async () => {
    await sendChat(page, FIRST);
    // The split picker, the way `panes.spec.ts` drives it: a second session,
    // bound to a pane, which is the arrangement this bug lives in.
    await page.keyboard.press("Alt+S");
    const dialog = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(dialog).toBeVisible();
    await dialog.locator(".wb-qb-row", { hasText: "New agent session" }).first().click();
    await expect(dialog).toBeHidden();
    await expect(page.locator(".wb-chat")).toHaveCount(2);
  });

  const own = page.locator(".dv-groupview", {
    has: page.locator(".wb-panel-tab", { hasText: SLOW }),
  });
  const tab = page.locator(".wb-panel-tab", { hasText: SLOW });

  await test.step("it finishes a turn while you are looking at the other one", async () => {
    const input = page.locator(".dv-active-group .wb-chat-input textarea");
    await input.fill(SLOW);
    await input.press("Enter");
    // Look away *before* the turn ends — that is what makes the marker mean
    // something. Clicking a row in the picker is the other way to view a
    // session, and it moves the keyboard off the pane without hiding it.
    await pane.locator(".wb-session-row", { hasText: FIRST }).click();
    await expect(own.locator(".wb-chat-header .wb-badge")).toContainText("Done", {
      timeout: 30_000,
    });
  });

  await test.step("looking at that pane is what spends the marker", async () => {
    // The regression. `focusSession` — the only thing a dedicated pane calls —
    // did not clear the flags, so this badge said "Done" for the rest of the
    // window's life however long you sat reading the conversation.
    await tab.click();
    await expect(own.locator(".wb-chat-header .wb-badge")).toContainText("Idle");
    // …and the picker row for it agrees, because both read the one record.
    await expect(pane.locator(".wb-session-row", { hasText: SLOW })).not.toContainText("Done");
  });
});
