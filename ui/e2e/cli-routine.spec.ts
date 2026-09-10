/**
 * PR-E's journey — the scripted morning routine.
 *
 * The claim this proves is not "the CLI exited 0". It is that **one file you
 * run puts the window into a working arrangement**: a script of four ops, run
 * in one process against the live backend, re-opens a workspace from the recent
 * list, switches to a layout the user saved by name, focuses a panel, and starts
 * an agent session with a prompt — and the *window* is then in that state.
 *
 * Three things are load-bearing here and nowhere else in the suite:
 *
 *  1. **`params` reaches `run()`.** Until PR-E the relay carried a `params`
 *     object that `executeCommandById` dropped on the floor, so `layout.switch`
 *     and `session.start` could not exist. Every parameterised op below fails on
 *     master.
 *  2. **`workspace.open` is narrowed for the CLI**, not widened. The second test
 *     drives a folder that exists, is readable, and is inside the workspace —
 *     and it is refused, because the user never opened it *as* a workspace. A
 *     path off the recent list is not resolved, and the window does not move.
 *     The first test drives the other half: a folder that *is* on the list,
 *     written with forward slashes the way a hand-written `routine.json` writes
 *     a Windows path, is accepted. The recent list is Python's backslashes, so a
 *     match on case alone refused the window's own workspace.
 *  3. **The batch mode is the feature.** The four-op script is measured against
 *     four separate invocations of the same binary; the assertion is on the
 *     ratio, because otherwise `--script` is a refactor with a flag.
 *
 * The routine deliberately re-opens **the workspace this window is already in**
 * (the launch workspace, which the server records on start). Re-rooting the E2E
 * server mid-suite is `workspaces.spec.ts`'s job and it puts it back; what this
 * journey is about is the *validation*, and the refusal half is asserted against
 * a path that can never be on the list.
 */

import child_process from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type Page } from "@playwright/test";

import { openApp } from "./app";
import { E2E_APP_DATA, E2E_WORKSPACE, SRC_DIR, workspacePath } from "./workspace";

/** Repo root: this file lives in `<root>/ui/e2e`. */
const REPO_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..");

/** The port the harness put the backend on (`playwright.config.ts`). */
const SERVER_PORT = process.env.WB_E2E_SERVER_PORT ?? "8788";

/** The layout this journey saves, switches away from, and switches back to. */
const MORNING = "Morning";

const PROMPT = "summarise the SE3 dispatch model";

interface CliRun {
  status: number;
  stdout: string;
  stderr: string;
  ms: number;
}

/**
 * Run `workbench-cmd` the way a user would — the real console script, the real
 * token file, the real HTTP relay. `WORKBENCH_APP_DATA_ROOT` is how it finds the
 * per-launch token this run's server dropped (the harness moved that root off
 * the developer's machine-local one).
 */
function workbenchCmd(args: string[], input?: string): CliRun {
  const started = Date.now();
  const result = child_process.spawnSync(
    "uv",
    ["run", "--project", REPO_ROOT, "workbench-cmd", "--port", SERVER_PORT, ...args],
    {
      cwd: REPO_ROOT,
      input: input ?? "",
      encoding: "utf-8",
      env: { ...process.env, WORKBENCH_APP_DATA_ROOT: E2E_APP_DATA },
      shell: true,
    },
  );
  return {
    status: result.status ?? -1,
    stdout: result.stdout ?? "",
    stderr: result.stderr ?? "",
    ms: Date.now() - started,
  };
}

/** The layout chip in the status bar — this window has exactly one. */
const layoutChip = (page: Page) => page.locator(".wb-layout-chip");

/** Open the layout menu (it is a dialog on the chip) and act in it. */
async function openLayoutMenu(page: Page): Promise<void> {
  await layoutChip(page).click();
  await expect(page.getByRole("dialog", { name: "Layouts" })).toBeVisible();
}

/** Save the current arrangement under `name`, through the chip's own menu. */
async function saveLayoutAs(page: Page, name: string): Promise<void> {
  await openLayoutMenu(page);
  const input = page.getByRole("textbox", { name: "Name for this arrangement" });
  await input.fill(name);
  await input.press("Enter");
  await expect(page.getByRole("dialog", { name: "Layouts" })).toHaveCount(0);
  await expect(layoutChip(page)).toContainText(name);
}

/** Put the window back: delete the saved layout and return to the default. */
async function cleanUpLayout(page: Page): Promise<void> {
  await openLayoutMenu(page);
  const remove = page.getByRole("button", { name: `Delete the ${MORNING} layout` });
  if ((await remove.count()) > 0) await remove.click();
  await page.getByRole("dialog", { name: "Layouts" }).getByText("Default", { exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Layouts" })).toHaveCount(0);
}

const scratchpad = (page: Page) => page.getByRole("textbox", { name: "Scratchpad" });

test("one script puts the window into the morning arrangement", async ({ page }) => {
  await openApp(page);

  // --- what the user did yesterday: an arrangement, saved under a name -------
  //
  // Driven through the app's own surfaces, because the thing under test is that
  // the CLI can reach a name *the window published* — seeding layouts.json
  // behind the window's back would prove the file format instead.
  await test.step("save an arrangement the routine can name", async () => {
    const resp = await page.request.post("/api/commands/invoke", {
      data: { command_id: "scratchpad.open" },
    });
    expect(resp.status()).toBe(200);
    await expect(scratchpad(page)).toBeVisible();
    await saveLayoutAs(page, MORNING);

    // …and then leave it, so switching back is a change and not a no-op.
    await openLayoutMenu(page);
    await page
      .getByRole("dialog", { name: "Layouts" })
      .getByText("Default", { exact: true })
      .click();
    await expect(scratchpad(page)).toHaveCount(0);
  });

  // --- the routine ----------------------------------------------------------
  //
  // The workspace path is taken from the server's own recent list rather than
  // from the harness constant: that list is what `workspace.open` validates
  // against, and it holds the *resolved* root, which on Windows need not be the
  // string `mkdtemp` handed back.
  const recentRoot = await test.step("the launch workspace is on the recent list", async () => {
    const resp = await page.request.get("/api/workspace");
    const body = (await resp.json()) as { root: string; recents: { path: string }[] };
    const found = body.recents.find((ref) => ref.path.toLowerCase() === body.root.toLowerCase());
    expect(found, "the server records its launch workspace as a recent").toBeDefined();
    return found?.path ?? body.root;
  });

  // Discovery, the way a person (or an agent) finds out what takes arguments:
  // the shape rides the listing, and only for the three that have one.
  await test.step("the listing shows which commands take arguments", () => {
    const listed = workbenchCmd(["list"]);
    expect(listed.status).toBe(0);
    // Whole lines, so "carries no hint" is assertable rather than assumed.
    const lines = listed.stdout.split(/\r?\n/);
    expect(lines).toContain("layout.switch :: Switch to a named layout  {name:str}");
    expect(lines.find((line) => line.startsWith("session.start ::"))).toContain(
      "{prompt:str,cwd:str?}",
    );
    expect(lines.find((line) => line.startsWith("workspace.open ::"))).toContain("{path:str}");
    // …and a parameterless one is exactly as it was: no schema, no bytes spent.
    expect(lines).toContain("panel.terminal :: Focus Terminal panel");
  });

  // **Written the way a person writes a Windows path in JSON.** The recent list
  // holds what Python's `str(Path(...))` produced — always backslashes — but a
  // backslash has to be doubled inside a JSON string, so the idiomatic
  // `routine.json` says `C:/work/alpha`. It is the same folder, and the routine
  // has to agree: matching on a lower-cased string alone made it a different one
  // and refused the window's own current workspace as "not on the recent list",
  // stopping the script at op 1. On a POSIX root this line is a no-op and the
  // op is unchanged.
  const recentRootAsWritten = recentRoot.replace(/\\/g, "/");

  const routine = JSON.stringify({
    ops: [
      { command_id: "workspace.open", params: { path: recentRootAsWritten } },
      { command_id: "layout.switch", params: { name: MORNING } },
      { command_id: "panel.agent" },
      { command_id: "session.start", params: { prompt: PROMPT, cwd: SRC_DIR } },
    ],
  });

  const script = workbenchCmd(["--script", "-"], routine);
  expect(script.stderr + script.stdout).toContain("4 ops");
  expect(script.status).toBe(0);

  // --- the window is in that state, which is the whole claim ----------------
  await expect(layoutChip(page)).toContainText(MORNING);
  await expect(scratchpad(page)).toBeVisible();
  // The session was created in `src/` and carries the prompt as its first turn.
  await expect(page.locator(".wb-sessions-folder").filter({ hasText: SRC_DIR })).toBeVisible();
  await expect(page.locator(".wb-msg-user").filter({ hasText: PROMPT })).toBeVisible();

  // --- the batch mode is a feature, not a refactor --------------------------
  //
  // Four separate invocations of the same binary, doing the cheapest op there
  // is. The ratio is what is asserted: each process pays its own interpreter
  // start, and the script pays one.
  let separate = 0;
  for (let i = 0; i < 4; i += 1) {
    const one = workbenchCmd(["run", "panel.terminal"]);
    expect(one.status).toBe(0);
    separate += one.ms;
  }
  // Printed, not only asserted: the number is the argument for `--script`
  // existing at all, and a lane that only sees a green tick cannot re-check it.
  console.log(
    `[cli-routine] 4-op script ${String(script.ms)}ms; ` +
      `four separate invocations ${String(separate)}ms`,
  );
  expect(
    script.ms,
    `script ${String(script.ms)}ms vs ${String(separate)}ms for four invocations`,
  ).toBeLessThan(separate * 0.6);

  await cleanUpLayout(page);
});

test("a folder that is not on the recent list is refused, and the window stays put", async ({
  page,
}) => {
  await openApp(page);

  // A real, readable directory *inside* the workspace — the friendliest possible
  // path — that the user has never opened as a workspace. The narrowing is about
  // who is asking, not about whether the folder is fine.
  const notRecent = workspacePath(SRC_DIR);
  const routine = JSON.stringify({
    ops: [
      { command_id: "panel.terminal" },
      { command_id: "workspace.open", params: { path: notRecent } },
      { command_id: "panel.agent" },
    ],
  });

  const run = workbenchCmd(["--script", "-"], routine);
  expect(run.status).not.toBe(0);
  const output = run.stdout + run.stderr;
  expect(output).toContain("recent");
  // Stopped at the failing op: the third never ran.
  expect(output).toContain("stopped at op 2");

  // The window is still looking at the workspace it was launched in.
  await expect(page.locator(".wb-workspace-chip")).toContainText(path.basename(E2E_WORKSPACE));
});
