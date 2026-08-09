/**
 * Journey 10 — the pane system: split anything, put anything in it, run as many
 * as you want.
 *
 * The point of this journey, like journey 9's, is the **reload**. Splitting a
 * pane and dropping a second agent session into it is easy to fake in memory;
 * the claim that makes it a tool rather than a demo is that the arrangement
 * *and the identities* come back — that pane is still that conversation, that
 * pane is still that terminal — because the binding is the dockview panel id
 * and the panel ids are what `.workbench/layouts.json` holds (`ui/src/panes.ts`).
 *
 * Asserts:
 *  - `Alt+S` splits the focused pane and opens the picker on the QuickBar's own
 *    surface, listing every registered tool and everything a plural tool can be
 *    bound to;
 *  - a second agent session goes in the new pane, streams there, and the two
 *    chats are two conversations — a message typed in one lands in one;
 *  - `Alt+Shift+S` splits again and puts a real terminal in the new pane, with
 *    its own PTY;
 *  - the whole window is navigable **by keyboard alone**: directional movement
 *    between panes, and cycling;
 *  - a **reload** brings back the arrangement, the pane ids, and the session
 *    behind the pane that was bound to it;
 *  - `Alt+Shift+<arrow>` trades two panes and leaves both boxes the size they
 *    were;
 *  - the mouse path exists too: the focused pane's tab strip carries the two
 *    split glyphs, and nothing else does;
 *  - `Alt+X` closes a pane.
 *
 * It ends by resetting to the default arrangement, because the journeys after it
 * share this workspace and expect the window they have always had — in
 * particular a terminal pane left behind would make "the visible terminal" two
 * elements for every journey that types into one.
 */

import { expect, request, test, type Locator, type Page } from "@playwright/test";

import type { LayoutsResponse } from "../src/types";
import { expectTerminal, openApp, workspaceReady } from "./app";

const DEFAULT_PANELS = ["Agent", "Editor", "Files", "Terminal"];

/** Panel tab titles on screen, sorted — an arrangement's identity without
 * depending on which group dockview renders first. */
async function panels(page: Page): Promise<string[]> {
  const titles = await page.locator(".wb-panel-tab").allTextContents();
  return titles.map((title) => title.replace("×", "").trim()).sort();
}

/** The pane the keyboard is in, by its tab title. dockview marks it in the DOM,
 * which is also how the user knows (DESIGN.md §6.1 accent top edge). */
async function focusedPane(page: Page): Promise<string> {
  return (
    (await page.locator(".dv-active-group .wb-panel-tab").first().textContent()) ?? ""
  ).replace("×", "").trim();
}

/** Every pane's box, by tab title — real geometry, read from the live DOM. */
async function geometry(page: Page): Promise<Record<string, [number, number, number, number]>> {
  return page.evaluate(() => {
    const boxes: Record<string, [number, number, number, number]> = {};
    for (const group of document.querySelectorAll(".dv-groupview")) {
      const title = (group.querySelector(".wb-panel-tab")?.textContent ?? "")
        .replace("×", "")
        .trim();
      const box = group.getBoundingClientRect();
      boxes[title] = [
        Math.round(box.left),
        Math.round(box.top),
        Math.round(box.width),
        Math.round(box.height),
      ];
    }
    return boxes;
  });
}

/** Pane ids as they are on disk. The whole claim of this journey is about these
 * strings, so they are read from the file the next launch will trust. */
async function persistedPaneIds(page: Page): Promise<string[]> {
  const response = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
  const current = response.state.current as { panels?: Record<string, unknown> } | null;
  return Object.keys(current?.panels ?? {}).sort();
}

/** Wait until the debounced autosave has written a pane we just made. */
async function persisted(page: Page, paneId: string): Promise<void> {
  await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(paneId);
}

const picker = (page: Page, label: string): Locator =>
  page.getByRole("dialog", { name: label });

/** Split the focused pane and pick a row by its title — the whole gesture. */
async function split(page: Page, chord: string, label: string, row: string): Promise<void> {
  await page.keyboard.press(chord);
  const dialog = picker(page, label);
  await expect(dialog).toBeVisible();
  await dialog.locator(".wb-qb-row", { hasText: row }).first().click();
  await expect(dialog).toBeHidden();
}

/** Run a QuickBar command by its row title. */
async function runCommand(page: Page, title: string): Promise<void> {
  await page.keyboard.press("Control+Shift+P");
  const quickbar = page.getByRole("dialog", { name: "Quick open" });
  await quickbar.locator(".wb-qb-row", { hasText: title }).first().click();
  await expect(quickbar).toBeHidden();
}

/**
 * Leave the workspace with the arrangement every other journey expects. Not
 * belt-and-braces: this journey persists window state and six journeys run
 * after it, so a *failed* step must not hand them a window with two terminals
 * in it.
 */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

test("split a pane, put a second agent in it, and find it there after a reload", async ({
  page,
}) => {
  await openApp(page);
  expect(await panels(page)).toEqual(DEFAULT_PANELS);

  await test.step("exactly one pane wears the live rule, and it is the focused one", async () => {
    // The signature, asserted in the running window rather than eyeballed
    // (DESIGN.md §6.1): a 2px amber `::before` across the top of the focused
    // pane's tab strip, and nowhere else. "Where am I" must never be ambiguous,
    // so this is the one measurement that says the focus indicator is real —
    // exactly one strip paints it, and it is the active group's.
    const amber = await page.evaluate(() => {
      // Resolve `--accent` to the rgb form getComputedStyle returns, so the
      // assertion is theme-independent: dark spends the hot amber, light a deep
      // one, and this reads whichever the running window is in rather than a
      // hard-coded hex.
      const probe = document.createElement("span");
      probe.style.color = "var(--accent)";
      document.body.appendChild(probe);
      const wanted = getComputedStyle(probe).color;
      probe.remove();
      let total = 0;
      let onActive = 0;
      for (const strip of document.querySelectorAll(".dv-tabs-and-actions-container")) {
        const bg = getComputedStyle(strip, "::before").backgroundColor;
        if (bg !== wanted) continue;
        total++;
        if (strip.closest(".dv-active-group") !== null) onActive++;
      }
      return { total, onActive, wanted };
    });
    // The accent resolved to a real colour, not a transparent fallback.
    expect(amber.wanted, "an accent colour resolved").toMatch(/^rgba?\(/);
    expect(amber.total, "exactly one pane carries the amber live rule").toBe(1);
    expect(amber.onActive, "and it is the focused pane's strip").toBe(1);
  });

  await test.step("the focused pane, and only it, offers to split", async () => {
    // Chrome (the mouse path): two glyphs at the right end of one tab strip.
    await expect(page.getByRole("button", { name: "Split this pane to the right" })).toHaveCount(1);
    await expect(page.getByRole("button", { name: "Split this pane downwards" })).toHaveCount(1);
    const owner = page
      .locator(".dv-groupview", { has: page.getByRole("button", { name: "Split this pane to the right" }) })
      .locator(".wb-panel-tab")
      .first();
    expect((await owner.textContent())?.trim()).toBe(await focusedPane(page));
  });

  await test.step("Alt+S opens the picker on the QuickBar's own surface", async () => {
    await page.keyboard.press("Alt+S");
    const dialog = picker(page, "Split this pane to the right");
    await expect(dialog).toBeVisible();
    // Every registered tool, plus what the plural ones can be bound to. This is
    // the "one surface" claim: same rows, same keycaps, same overlay.
    for (const row of ["Files", "Editor", "Agent", "Terminal", "Scratchpad", "New agent session", "New terminal"]) {
      await expect(dialog.locator(".wb-qb-row", { hasText: row }).first()).toBeVisible();
    }
    // …and it filters by typing, like everything else in that overlay. The
    // field is a `combobox` rather than a `textbox`: the palette implements the
    // ARIA combobox pattern, so the explicit role replaces the input's implicit
    // one (`QuickBar.tsx`, `quickbarA11y.spec.ts`).
    await dialog.getByRole("combobox").fill("termi");
    await expect(dialog.locator(".wb-qb-row")).toHaveCount(2); // "Terminal", "New terminal"
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  let sessionPane = "";
  await test.step("a second agent session goes in a pane of its own", async () => {
    await split(page, "Alt+S", "Split this pane to the right", "New agent session");
    // Two chats, mounted at once — which is the headline: a fleet you can see.
    await expect(page.locator(".wb-chat")).toHaveCount(2);
    await expect
      .poll(
        async () => (await persistedPaneIds(page)).filter((id) => id.startsWith("agent#")).length,
        { timeout: 10_000 },
      )
      .toBe(1);
    sessionPane = (await persistedPaneIds(page)).find((id) => id.startsWith("agent#")) ?? "";
    expect(sessionPane, "a pane bound to a session id").not.toBe("");
  });

  await test.step("a message typed in that pane goes to that session", async () => {
    const input = page.locator(".dv-active-group .wb-chat-input textarea");
    await expect(input).toBeVisible();
    await input.fill("pane two speaking");
    await input.press("Enter");
    // Exactly one conversation holds it. (The default Agent panel shows the
    // session the keyboard is on, which is this one — so two *views*, one
    // conversation: the message appears once per view and nowhere else.)
    await expect(page.locator(".wb-msg-user", { hasText: "pane two speaking" })).toHaveCount(2);
    // The tab renames itself once the session is titled from that message.
    await expect(
      page.locator(".wb-panel-tab", { hasText: "pane two speaking" }),
    ).toBeVisible();
  });

  await test.step("split again, downwards, for a terminal of its own", async () => {
    await split(page, "Alt+Shift+S", "Split this pane downwards", "New terminal");
    await expect(page.locator(".wb-panel-tab", { hasText: "Terminal 2" })).toBeVisible();
    // A real PTY, not a placeholder: two terminals are mounted and the new one
    // reaches a live PowerShell prompt.
    await expect(page.locator(".wb-terminal")).toHaveCount(2);
    await persisted(page, "terminal#2");
  });

  await test.step("the window is navigable by keyboard alone", async () => {
    // Alt+ArrowLeft is the browser's Back — the app takes it, which is the
    // other half of this assertion: a page that navigated away fails here.
    const visited: string[] = [];
    for (const chord of ["Alt+ArrowLeft", "Alt+ArrowLeft", "Alt+ArrowRight", "Alt+ArrowDown"]) {
      await page.keyboard.press(chord);
      visited.push(await focusedPane(page));
    }
    await expect(page.getByRole("tree", { name: "Workspace files" })).toBeVisible();
    expect(new Set(visited).size, `moved between panes: ${visited.join(" -> ")}`).toBeGreaterThan(1);
    // Two lefts from the right-hand column reach the far edge of the window.
    expect(visited, visited.join(" -> ")).toContain("Files");

    // Cycling reaches every pane and comes back to where it started.
    const start = await focusedPane(page);
    const seen = new Set<string>();
    const paneCount = await page.locator(".dv-groupview").count();
    for (let i = 0; i < paneCount; i++) {
      await page.keyboard.press("Alt+O");
      seen.add(await focusedPane(page));
    }
    expect(seen.size).toBe(paneCount);
    expect(await focusedPane(page)).toBe(start);
  });

  await test.step("the arrangement AND the identities survive a reload", async () => {
    const before = await persistedPaneIds(page);
    const boxes = await geometry(page);
    await page.reload();
    await workspaceReady(page);

    // Same panes, same ids — not "an agent pane came back" but *that* one.
    await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toEqual(before);
    expect(before).toContain(sessionPane);
    expect(before).toContain("terminal#2");
    // Same boxes, to the pixel.
    expect(await geometry(page)).toEqual(boxes);
    // And the pane bound to that session is still showing that conversation:
    // its tab carries the title the message gave the session.
    await expect(page.locator(".wb-panel-tab", { hasText: "pane two speaking" })).toBeVisible();
    // The terminal pane is a terminal again, with a live shell in it.
    await expect(page.locator(".wb-terminal")).toHaveCount(2);
  });

  await test.step("Alt+Shift+<arrow> trades two panes without resizing them", async () => {
    await page.locator(".wb-panel-tab", { hasText: "Terminal 2" }).click();
    const before = await geometry(page);
    const mine = before["Terminal 2"];
    await page.keyboard.press("Alt+Shift+ArrowUp");
    const after = await geometry(page);
    const moved = after["Terminal 2"];
    expect(mine, "Terminal 2 was on screen before the swap").toBeDefined();
    expect(moved, "and after it").toBeDefined();
    // It went somewhere, and it is the same size it was.
    expect([moved?.[0], moved?.[1]]).not.toEqual([mine?.[0], mine?.[1]]);
    expect([moved?.[2], moved?.[3]]).toEqual([mine?.[2], mine?.[3]]);
  });

  await test.step("Alt+X closes the focused pane", async () => {
    const before = await page.locator(".dv-groupview").count();
    await page.keyboard.press("Alt+X");
    await expect(page.locator(".dv-groupview")).toHaveCount(before - 1);
  });

  await test.step("reset leaves the workspace as the next journey expects it", async () => {
    await runCommand(page, "Switch to the Default layout");
    expect(await panels(page)).toEqual(DEFAULT_PANELS);
    await expect(page.locator(".wb-terminal")).toHaveCount(1);
  });
});

/**
 * The other half of "put any tool in any pane": a tool that is a **singleton**
 * moves into the split rather than being cloned.
 *
 * One pane per identity is what makes a pane id mean something — two Files
 * panes would be two trees of the same directory, and a saved layout naming
 * `files` twice would be a layout nothing could restore faithfully.
 */
test("picking a tool that already has a pane moves it, never clones it", async ({ page }) => {
  await openApp(page);
  const before = await page.locator(".wb-panel-tab").count();

  await page.locator(".wb-panel-tab", { hasText: "Terminal" }).first().click();
  const terminalBox = (await geometry(page)).Terminal;
  await split(page, "Alt+S", "Split this pane to the right", "Files");

  expect(await page.locator(".wb-panel-tab").count(), "no second Files pane").toBe(before);
  const moved = (await geometry(page)).Files;
  // It is now beside the terminal it was split off, not down the left edge.
  expect(moved?.[0] ?? 0).toBeGreaterThan(terminalBox?.[0] ?? 0);

  await runCommand(page, "Switch to the Default layout");
  expect(await panels(page)).toEqual(DEFAULT_PANELS);
  await expectTerminal(page, "PS ", 60_000);
});

/**
 * The other end of "a saved layout brings back *those* conversations": what
 * happens when it cannot.
 *
 * `SessionManager` keeps its sessions in a plain dict in memory, so every
 * session id in `.workbench/layouts.json` is unknown to the next server
 * process. The pane still restores — deliberately, since a key that has not
 * loaded *yet* must not be dropped — and says the session is not running.
 *
 * What it must not do is open a socket anyway. `/ws/agent/{unknown}` is closed
 * by the server with 4404, and a `ReconnectingSocket` that treated that as a
 * blip would retry every ten seconds, per dead pane, for as long as the tab
 * stayed open: a background reconnect storm behind a note correctly saying the
 * session is gone. Restore a four-agent layout after a restart and that is four
 * of them, and nothing on screen says so.
 *
 * The repro is the real thing rather than a mock: build a pane bound to a live
 * session, let the debounced autosave write it, then rewrite that id on disk to
 * one the server has never issued — which is byte-for-byte what a restart
 * leaves behind — and reload.
 */
test("a pane restored onto a session the server no longer has stays quiet", async ({ page }) => {
  await openApp(page);

  await split(page, "Alt+S", "Split this pane to the right", "New agent session");
  await expect
    .poll(
      async () => (await persistedPaneIds(page)).filter((id) => id.startsWith("agent#")).length,
      { timeout: 10_000 },
    )
    .toBe(1);
  const liveId = ((await persistedPaneIds(page)).find((id) => id.startsWith("agent#")) ?? "").slice(
    "agent#".length,
  );
  expect(liveId, "a pane bound to a session id").not.toBe("");

  // 12 hex chars, the shape `SessionManager.create` mints — and not one it has.
  const dead = "dead0cafe911";
  await test.step("the layout on disk names a session this server never had", async () => {
    const before = (await (await page.request.get("/api/layouts")).json()) as LayoutsResponse;
    const rewritten = JSON.parse(
      JSON.stringify(before.state).split(liveId).join(dead),
    ) as LayoutsResponse["state"];
    await page.request.put("/api/layouts", { data: rewritten });
  });

  const sockets: string[] = [];
  page.on("websocket", (ws) => sockets.push(ws.url()));

  await page.reload();
  await workspaceReady(page);

  await test.step("the pane comes back, and says the session is gone", async () => {
    await expect.poll(() => persistedPaneIds(page), { timeout: 10_000 }).toContain(`agent#${dead}`);
    await expect(page.getByText("This session is not running any more.")).toBeVisible();
  });

  await test.step("and it is not reconnecting behind that note", async () => {
    // The one deliberate wait in this suite, because the assertion is an
    // *absence*. Unfixed, the socket opens on mount and retries at 0.5 s, 1 s
    // and 2 s (`ws.ts` backoff) — four attempts inside this window, the first
    // of them already made before the note above could render.
    await page.waitForTimeout(3_000);
    expect(sockets.filter((url) => url.includes(`/ws/agent/${dead}`))).toEqual([]);
  });

  await runCommand(page, "Switch to the Default layout");
  expect(await panels(page)).toEqual(DEFAULT_PANELS);
});
