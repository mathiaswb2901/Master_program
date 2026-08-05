/**
 * Journey 3 — QuickBar, shortcuts.md and the never-run rule.
 *
 * Asserts:
 *  - the malformed entry seeded into `<workspace>/.workbench/shortcuts.md`
 *    surfaces as a problems toast rather than dying silently, and is carried by
 *    the shortcuts payload itself (the half of the assertion that outlives the
 *    toast's auto-dismiss);
 *  - Ctrl+P searches files, and a cargo build tree is not among them — while a
 *    folder that merely shares its name is;
 *  - Ctrl+Shift+P opens the QuickBar in command mode, with keycaps on the rows
 *    that carry a chord;
 *  - file-supplied shortcuts appear under their own "Shortcuts" category;
 *  - running a shell shortcut *types* the snippet into the terminal and stops
 *    there: the text sits on the prompt line, and the shell produced no output
 *    for it (the snippet appears exactly once — an executed `echo` would print
 *    its argument a second time);
 *  - a **registered tool** reaches the user through the registry alone: the
 *    Scratchpad's command is in the QuickBar, running it opens a panel that is
 *    not in the default layout, what is typed there lands in a real workspace
 *    file, and the tab it arrived on closes it again.
 */

import path from "node:path/posix";

import { expect, test } from "@playwright/test";

import type { FileContent, ShortcutsState } from "../src/types";
import { gotoApp, runInTerminal, terminal, terminalText, treeItem, workspaceReady } from "./app";
import {
  BROKEN_SHORTCUT_NAME,
  OWN_TARGET_FILE,
  SHORTCUT_BODY,
  SHORTCUT_NAME,
} from "./workspace";

const MARKER = "e2e-shortcut-marker";
/** The demo tool's own file, and a note to prove it really lands there. */
const SCRATCH_FILE = ".workbench/scratch.md";
const SCRATCH_NOTE = "SE3 spread looked odd on the 25-hour day.";
/** Sync command for the never-run step. Its output differs from its input, and
 * shares no prefix with `workbench-e2e-<random>` on the prompt line. */
const SYNC_COMMAND = 'echo "sync-$(4+5)"';
const SYNC_OUTPUT = "sync-9";

test("command mode, shortcut categories, and a snippet that never runs", async ({ page }) => {
  await test.step("the malformed entry is reported, not swallowed", async () => {
    // Asserted before the tree waits, and in one expect: the toast is raised
    // when the shortcuts fetch resolves and auto-dismisses ~6 s later
    // (TOAST_AUTO_DISMISS_MS), which is well inside `workspaceReady`'s own
    // budget. What it says is then re-read from the payload, which never
    // expires — so a slow load can cost the presence check, never the content.
    await gotoApp(page);
    await expect(page.locator(".wb-toast.is-warn")).toContainText(
      `.workbench/shortcuts.md: ${BROKEN_SHORTCUT_NAME}`,
    );

    const response = await page.request.get("/api/shortcuts");
    expect(response.ok()).toBe(true);
    const state = (await response.json()) as ShortcutsState;
    // Scoped to the workspace file: the server also merges the *developer's*
    // `~/.workbench/shortcuts.md`, which is not a setting and so cannot be
    // pointed elsewhere for a test run. Whatever it holds is not this
    // assertion's business — the seeded file must contribute this one problem.
    const workspaceProblems = state.problems
      .filter((problem) => problem.file === ".workbench/shortcuts.md")
      .map((problem) => problem.message);
    expect(workspaceProblems).toEqual([expect.stringContaining(`${BROKEN_SHORTCUT_NAME}:`)]);
  });

  await workspaceReady(page);

  await test.step("build artifacts are not files you can open; same-named folders are", async () => {
    // The bug this replaces: a Tauri build put ~3.5k files under
    // `desktop/src-tauri/target/`, and every one of them was offered here.
    await page.keyboard.press("Control+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await expect(quickbar.locator(".wb-qb-input")).toHaveValue("");

    await page.keyboard.type("popup");
    await expect(quickbar.locator(".wb-qb-empty")).toHaveText("No matching files");

    // The other half, and the reason the name `target` was not simply banned:
    // the analyst's own folder of target data is untouched.
    await quickbar.locator(".wb-qb-input").fill("se3-targets");
    const row = quickbar.locator(".wb-qb-row").first();
    await expect(row).toContainText("se3-targets-2026.csv");
    await expect(row).toContainText(path.dirname(OWN_TARGET_FILE));

    await page.keyboard.press("Escape");
    await expect(quickbar).toBeHidden();
  });

  await test.step("the file tree does not walk the build cache either", async () => {
    await expect(treeItem(page, "desktop")).toBeVisible();
    await treeItem(page, "desktop").click();
    await expect(treeItem(page, "src-tauri")).toBeVisible();
    await treeItem(page, "src-tauri").click();
    // `target` is the only thing in it, and it is gone.
    await expect(page.getByRole("treeitem", { name: "target" })).toHaveCount(0);
  });

  await test.step("Ctrl+Shift+P opens command mode with keycaps", async () => {
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await expect(quickbar).toBeVisible();
    await expect(quickbar.locator(".wb-qb-input")).toHaveValue(">");
    const newTerminalRow = quickbar.locator(".wb-qb-row", { hasText: "New terminal" }).first();
    await expect(newTerminalRow.locator(".wb-keycap")).toHaveText(["Alt", "T"]);
  });

  await test.step("shortcuts.md entries get their own category", async () => {
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    // Both categorized sections, in order: the registry's dynamic rows (the
    // Layouts tool's) come before the file's, which is what keeps a section
    // header a header rather than something that appears mid-list.
    await expect(quickbar.locator(".wb-qb-cat")).toHaveText(["Layouts", "Shortcuts"]);
    const row = quickbar.locator(".wb-qb-row", { hasText: SHORTCUT_NAME }).first();
    // The row shows the snippet itself, never the file's own description.
    await expect(row).toContainText(SHORTCUT_BODY);
  });

  await test.step("running the shortcut types it without executing it", async () => {
    await page
      .getByRole("dialog", { name: "Quick open" })
      .locator(".wb-qb-row", { hasText: SHORTCUT_NAME })
      .first()
      .click();
    await expect(terminal(page).locator(".xterm-rows")).toContainText(MARKER);
    // The line it sits on is the live prompt, not a finished command's output.
    const typed = await terminalText(page);
    expect(typed.slice(typed.lastIndexOf("PS "))).toContain(SHORTCUT_BODY);

    // Counting now would prove nothing: had the snippet been executed, its
    // output and the next prompt would still be a PTY round-trip away, so the
    // screen would look the same either way. Ctrl+C abandons the typed line
    // (Enter is the one key this journey may never send) and the sync command's
    // output can only reach the screen after the shell has finished everything
    // queued before it — including an execution that should not have happened.
    await terminal(page).locator(".xterm-screen").click();
    await page.keyboard.press("Control+c");
    await runInTerminal(page, SYNC_COMMAND);
    await expect(terminal(page).locator(".xterm-rows")).toContainText(SYNC_OUTPUT);

    const text = await terminalText(page);
    const occurrences = text.split(MARKER).length - 1;
    expect(occurrences, "the snippet was typed and never run").toBe(1);
  });

  await test.step("a registered tool brings its own command and panel", async () => {
    // The exit criterion of the tool registry, end to end. Scratchpad is one
    // module plus one line in `ui/src/tools.ts` — no App.tsx, no commands.ts,
    // no CSS bundle — and this is what that buys: a QuickBar row, a panel that
    // is not in the default layout until the command opens it, and a real file
    // on disk behind it.
    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    const row = quickbar.locator(".wb-qb-row", { hasText: "Open Scratchpad" }).first();
    // No keycaps: the demo tool deliberately claims no chord, because a
    // registered one would take an Alt chord away from the user's own
    // shortcuts.md (which may bind nothing else).
    await expect(row.locator(".wb-keycap")).toHaveCount(0);

    await expect(page.getByRole("textbox", { name: "Scratchpad" })).toHaveCount(0);
    await row.click();
    const pad = page.getByRole("textbox", { name: "Scratchpad" });
    await expect(pad).toBeVisible();

    await pad.fill(SCRATCH_NOTE);
    await pad.blur();
    await expect(page.locator(".wb-scratchpad-status")).toHaveText("Saved");
    const saved = await page.request.get(
      `/api/files/content?path=${encodeURIComponent(SCRATCH_FILE)}`,
    );
    expect(saved.ok()).toBe(true);
    expect(((await saved.json()) as FileContent).content).toBe(SCRATCH_NOTE);
  });

  await test.step("and the panel it opened can be closed again", async () => {
    // The other half of the on-demand path, and the reason it is asserted: the
    // Scratchpad splits the right-hand side away from the Agent panel, so a
    // panel that opens and cannot close leaves the layout broken for the rest
    // of the session (there is no layout persistence yet). Fixed panels keep
    // their chrome-only tabs — only an on-demand one gets this button.
    await page.getByRole("button", { name: "Close Scratchpad" }).click();
    await expect(page.getByRole("textbox", { name: "Scratchpad" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Close Agent" })).toHaveCount(0);
  });
});
