/**
 * Journey 8 — provenance: reviewing what an agent changed.
 *
 * The fake agent writes a real file with a real `Write` tool call; the real
 * watcher notices; the correlator attributes it. Everything asserted here is
 * the user-visible end of that:
 *  - the tree marks the file, naming the session in its accessible name;
 *  - opening the file raises the bar that says who changed it, with what, when;
 *  - opening is the acknowledgment — the dots clear, the bar stays (it answers
 *    "who wrote this", not "have you looked": DESIGN.md §6.1);
 *  - the bar's link opens that exact conversation in the Agent panel (the loop
 *    the chat's file links start, closed from the other side);
 *  - Dismiss puts the bar away, and a reload leaves it away.
 *
 * And the half a badge is worthless without — the changes that are *not* the
 * agent's. A wrong attribution is worse than none, so the journey also drives
 * the two ways the app is told "this one was not them": a `Write` the agent
 * announced and never completed, followed by the user making that exact change
 * outside the app; and the user's own editor save, both of a fresh file and of
 * the very file the agent had written. None of those may mark anything — and
 * each of them would, against an implementation that simply credited the most
 * recently active session.
 */

import { expect, test, type Page } from "@playwright/test";

import { newSession, openApp, sendChat, treeMenu, typeInEditor, workspaceReady } from "./app";
import { readWorkspaceFile, writeWorkspaceFile } from "./workspace";

/** Must match `WRITE_TARGET_NAME` in services/fake_agent.py. */
const WRITTEN = "written-by-agent.md";
/** Must match `REFUSED_WRITE_TARGET_NAME`: announced, never written. */
const REFUSED = "refused-by-agent.md";
/** A file only the user ever touches. */
const USER_FILE = "src/user-typed.md";

/**
 * `Ctrl+S`, and wait for the save to be *observably* over.
 *
 * The dirty flag is the app's own answer, and it has to be read from both sides
 * to mean anything. `store.saveFile` clears `dirty` only once
 * `PUT /api/files/content` has resolved, and the server writes the bytes
 * (tmp file + `os.replace`) before it answers — so a tab that *was* dirty and is
 * not any more is proof that the content is on disk, and the read that follows
 * needs no polling at all.
 *
 * Asserting only the second half is what made this flaky. "No dirty tab" is
 * equally true of an editor that never received the keystrokes, so a run where
 * the typing did not land sailed past that assertion and then sat out the full
 * 15 s poll waiting for bytes nobody had asked anyone to write — a timeout whose
 * message points at the filesystem and not at the missing keystroke.
 */
async function saveActiveEditor(page: Page): Promise<void> {
  const dirty = page.locator(".wb-editor-tab.is-dirty");
  await expect(dirty, "the keystrokes reached the buffer").toHaveCount(1);
  await page.keyboard.press("Control+s");
  await expect(dirty, "the save round-trip completed").toHaveCount(0);
}

const SESSION_PROMPT = "write file please and note the provenance";
/** A second conversation, so the link has somewhere to come back from. */
const OTHER_PROMPT = "an unrelated second conversation";
const REFUSED_PROMPT = "refuse write and leave the file alone";
/** A second real write, in the same conversation — distinct text, because
 * `sendChat` locates the turn it just sent by its own words. */
const SECOND_WRITE_PROMPT = "write file once more, please";

test("an agent's file change is attributed, reviewable and acknowledged", async ({ page }) => {
  await openApp(page);
  await newSession(page);

  await test.step("the agent writes a file for real", async () => {
    await sendChat(page, SESSION_PROMPT);
    const row = page.locator(".wb-tool").filter({ hasText: "Write" });
    await expect(row.locator(".wb-tool-name")).toHaveText("Write");
    // Bytes on disk, not just a chat row: this is what the watcher reports.
    await expect.poll(() => readWorkspaceFile(WRITTEN)).toContain("Written by the fake agent");
  });

  const marked = page.getByRole("treeitem", { name: new RegExp(WRITTEN) });

  await test.step("the tree marks it, and names the session that did it", async () => {
    await expect(marked).toBeVisible();
    // The dot is never the only signal — the accessible name carries the claim.
    await expect(marked.getByRole("img")).toHaveAttribute(
      "aria-label",
      new RegExp(`Changed by ${SESSION_PROMPT}.*\\(Write\\)`),
    );
  });

  await test.step("opening it says who changed it, with what, and how long ago", async () => {
    await marked.click();
    const bar = page.locator(".wb-provenance-bar");
    await expect(bar).toBeVisible();
    await expect(bar).toContainText(SESSION_PROMPT);
    await expect(bar).toContainText("Write");
    await expect(bar).toContainText("just now");
  });

  await test.step("opening acknowledges: the dots clear, the bar stays", async () => {
    await expect(marked.getByRole("img")).toHaveCount(0);
    await expect(page.locator(".wb-tree-agent-dot")).toHaveCount(0);
    await expect(page.locator(".wb-tab-agent-dot")).toHaveCount(0);
    // The bar is not an unread marker: it answers "who wrote what I am reading"
    // and carries the only link back to that conversation (DESIGN.md §6.1).
    await expect(page.locator(".wb-provenance-bar")).toBeVisible();
  });

  await test.step("the bar links back to the exact session that changed it", async () => {
    // Move to a different conversation first, so "it opened" cannot be true by
    // accident — the panel has to actually switch back.
    await newSession(page);
    await sendChat(page, OTHER_PROMPT);
    const original = page.locator(".wb-msg-user").filter({ hasText: SESSION_PROMPT });
    const other = page.locator(".wb-msg-user").filter({ hasText: OTHER_PROMPT });
    await expect(original).toHaveCount(0); // we are somewhere else now

    await page.getByRole("button", { name: `Open session ${SESSION_PROMPT}` }).click();
    // Asserted on the conversation itself, not on the header label: the panel
    // is showing that exact history, not an empty shell with the right name.
    await expect(original).toBeVisible();
    await expect(other).toHaveCount(0);
  });

  await test.step("a write the agent announced but never made is not attributed", async () => {
    // The agent says it will write this path and comes back with an error —
    // a declined permission card, or an Edit whose old string was not found.
    await sendChat(page, REFUSED_PROMPT);
    await expect(
      page.locator(".wb-tool-row.is-failed").filter({ hasText: REFUSED }),
    ).toBeVisible();

    // Now the user makes that change themselves, from outside the app and well
    // inside the correlator's window: another editor, or `git checkout`.
    writeWorkspaceFile(REFUSED, "# the user wrote this, not the agent\n");

    const refused = page.getByRole("treeitem", { name: new RegExp(REFUSED) });
    await expect(refused).toBeVisible();
    await expect(refused.getByRole("img")).toHaveCount(0);
    await refused.click();
    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);
    await expect(page.locator(".wb-tree-agent-dot")).toHaveCount(0);
  });

  await test.step("a file the user makes and saves is never marked", async () => {
    await treeMenu(page, "src", "New file…");
    const nameInput = page.getByRole("textbox", { name: "New file name" });
    await nameInput.fill("user-typed.md");
    await nameInput.press("Enter");
    await typeInEditor(page, "the user typed this");
    await saveActiveEditor(page);
    expect(readWorkspaceFile(USER_FILE)).toContain("the user typed this");

    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);
    await expect(page.locator(".wb-tree-agent-dot")).toHaveCount(0);
  });

  await test.step("the user's own save takes back a file the agent had written", async () => {
    await marked.click();
    await expect(page.locator(".wb-provenance-bar")).toBeVisible();
    await typeInEditor(page, "and then the user edited it\n");
    await saveActiveEditor(page);
    // The claim is retracted, not merely acknowledged: the bar goes with it.
    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);
    await expect(page.locator(".wb-tree-agent-dot")).toHaveCount(0);
  });

  await test.step("a fresh agent change raises it again, and Dismiss ends it for good", async () => {
    await sendChat(page, SECOND_WRITE_PROMPT);
    const bar = page.locator(".wb-provenance-bar");
    await expect(bar).toBeVisible();
    await bar.getByRole("button", { name: "Dismiss" }).click();
    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);

    // Dismissal is a decision about that file, not about this page load.
    await page.reload();
    await workspaceReady(page);
    await page.getByRole("treeitem", { name: new RegExp(WRITTEN) }).click();
    await expect(page.locator(".wb-editor-tab").filter({ hasText: WRITTEN })).toBeVisible();
    await expect(page.locator(".wb-provenance-bar")).toHaveCount(0);
  });
});
