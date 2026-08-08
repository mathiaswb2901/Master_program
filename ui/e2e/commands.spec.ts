/**
 * Commands the window does not own (M5 item 14), end to end.
 *
 * A command registered today must be invocable from outside the window with no
 * per-command wiring. This drives the *shell* half of that exit criterion the
 * way the CLI and the agent tool do — a plain `POST /api/commands/invoke`,
 * token-authed (the harness attaches the launch token to `page.request.*`) — and
 * proves the window actually ran it: the Scratchpad pane, which is not in the
 * default layout, appears. The relay path is real throughout — the browser
 * published its manifest on connect, received the `command_invoke` over
 * `/ws/events`, ran the same `run()` the QuickBar uses, and reported back.
 */

import { expect, test } from "@playwright/test";

import { openApp } from "./app";

test("a command invoked over HTTP runs in the window and its pane appears", async ({ page }) => {
  await openApp(page);

  // The window publishes its manifest on connect; wait until the backend has it
  // so the invoke below validates against a live window rather than racing it.
  await expect
    .poll(async () => {
      const resp = await page.request.get("/api/commands");
      const body = (await resp.json()) as { commands: { id: string }[] };
      return body.commands.map((c) => c.id);
    })
    .toContain("scratchpad.open");

  // The Scratchpad is not in the startup layout, so this is a clean before/after.
  const pad = page.getByRole("textbox", { name: "Scratchpad" });
  await expect(pad).toHaveCount(0);

  const resp = await page.request.post("/api/commands/invoke", {
    data: { command_id: "scratchpad.open" },
  });
  expect(resp.status()).toBe(200);
  const result = (await resp.json()) as { ok: boolean; dispatched: boolean };
  expect(result.dispatched).toBe(true);
  expect(result.ok).toBe(true);

  // The window ran it: the pane is on screen.
  await expect(pad).toBeVisible();
});

test("an unregistered id is refused with a 404, never run", async ({ page }) => {
  await openApp(page);
  const resp = await page.request.post("/api/commands/invoke", {
    data: { command_id: "definitely.not.a.command" },
  });
  expect(resp.status()).toBe(404);
});
