/**
 * The launch token handshake, proven through the *real* app.
 *
 * `bootstrap.test.ts` proves the token/api/ws seam in isolation; this proves
 * the one path that seam cannot reach — the dock's own startup. Layouts'
 * `onDockReady` fires an unconditional `GET /api/layouts` to restore the saved
 * arrangement, and dockview mounts that handler as a child of `App`, before
 * `App`'s own effects run. So without a gate, that restore GET is *guaranteed*
 * to beat the handshake to the wire and carry no token — silent with
 * enforcement off, a 403 on every launch with it on (PR4), which reverts the
 * user's saved layout to the default every time they open the window.
 *
 * The fix (`App.tsx`) paints the default arrangement immediately but hands the
 * dock to the registry — the act that starts restore — only once `initToken`
 * has resolved. This journey makes the ordering a fact of the run by holding
 * the handshake on the wire: an ungated restore would fire during the hold and
 * arrive tokenless, so a token on the restore GET is the app having waited.
 */

import { expect, test } from "@playwright/test";

import { gotoApp, workspaceReady } from "./app";

const TOKEN_HEADER = "x-workbench-token";
/** Longer than the launch takes, so the restore GET is always on the far side
 * of the handshake rather than usually on the far side of a race. */
const HANDSHAKE_DELAY_MS = 1_500;

test("the layout-restore GET waits for the handshake and carries the token", async ({ page }) => {
  // Hold the token handshake. An ungated restore fires immediately on dock
  // mount and would land here, tokenless, well inside this hold.
  await page.route("**/api/auth/token", async (route) => {
    await page.waitForTimeout(HANDSHAKE_DELAY_MS);
    await route.continue();
  });

  // The restore call, caught the moment it leaves — before any response, so its
  // request headers are read as sent.
  const restore = page.waitForRequest(
    (request) => request.url().includes("/api/layouts") && request.method() === "GET",
  );

  await gotoApp(page);

  const request = await restore;
  const token = request.headers()[TOKEN_HEADER];
  // Present and non-empty: the app resolved the handshake before letting the
  // dock restore, so the token the server would enforce on is on the request.
  expect(token, "restore GET carried no token — it beat the handshake").toBeTruthy();

  await page.unroute("**/api/auth/token");
  // The window still comes up — the gate delays restore, it does not break it.
  await workspaceReady(page);
});
