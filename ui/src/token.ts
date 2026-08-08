/**
 * The per-launch auth token, fetched once at startup and attached to every REST
 * call (`api.ts`) and every WebSocket (`ws.ts`, `Terminal.tsx`).
 *
 * Two ways in, decided by the host (M5 item 8, PR2):
 *
 *  - the Tauri shell hands it over directly (`authToken()` in `shell.ts`), so
 *    the desktop window never has to make the handshake request;
 *  - a browser tab fetches `GET /api/auth/token` same-origin. That endpoint is
 *    the one route exempt from the token requirement (chicken-and-egg), guarded
 *    on local Origin+Host instead (`routers/auth.py`). Same-origin rides the
 *    existing `/api` proxy, so it works in prod, `npm run dev`, and the
 *    E2E/perf preview alike.
 *
 * The token is never put in a URL or query string — it travels in a request
 * header and in the WebSocket subprotocol, never anywhere a proxy log or the
 * browser history would keep it.
 *
 * Enforcement is still OFF on the server this PR ships against (PR1 shipped
 * `enforce_auth=False`), so the server ignores what we send. Fetching and
 * attaching it now is what lets PR4 flip enforcement on without a client change.
 */

import { apiUrl } from "./backend";
import { authToken } from "./shell";
import type { AuthTokenResponse } from "./types";

let token: string | null = null;

/**
 * Fetch the token and hold it for the session. Called once, before the first
 * REST call or socket opens (`App.tsx`). Never throws: a failed handshake
 * leaves the token null, which (enforcement off) changes nothing, and (PR4,
 * enforcement on) is the same outcome as a token the server would reject — the
 * request fails at the call site, not here.
 */
export async function initToken(): Promise<void> {
  try {
    const fromShell = await authToken();
    if (fromShell !== null) {
      token = fromShell;
      return;
    }
    // Same-origin in a browser (the shell path returned non-null above and
    // never reaches here); through the backend-origin seam so a shell that
    // could not hand over a token still hits 127.0.0.1:<port>, not the asset
    // protocol (`backend.ts`).
    const res = await fetch(apiUrl("/api/auth/token"));
    if (!res.ok) return;
    const body = (await res.json()) as AuthTokenResponse;
    token = body.token;
  } catch {
    // No token: enforcement is off, so this is a no-op today. Left null on
    // purpose rather than retried — the next launch fetches it again.
  }
}

/** The token, or null before `initToken()` has resolved (or if it could not). */
export function getToken(): string | null {
  return token;
}
