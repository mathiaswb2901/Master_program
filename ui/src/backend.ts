/**
 * The single seam that decides *where* the backend lives — the one place the UI
 * turns a path (`/api/…`, `/ws/…`) into a URL it can actually reach.
 *
 * Two hosts, one rule (M4 packaging):
 *
 *  - **Browser tab** — the page was served by the backend (prod), by Vite's
 *    proxy (`npm run dev`), or by `vite preview` (the E2E/perf lane). In every
 *    one of those the backend is *same-origin*, so the origin stays `""` and
 *    `apiUrl("/api/x")` is the relative URL the app has always used. The browser
 *    path is byte-for-byte unchanged — that is the proof the fix did not
 *    regress dev.
 *  - **Tauri shell** — the built bundle serves the UI from the asset protocol
 *    (`tauri://localhost` / `https://tauri.localhost`), which is **not**
 *    same-origin with the Python backend on `127.0.0.1:<port>`. A relative
 *    `/api/x` would resolve against the asset protocol and never reach the
 *    server. The shell knows the port it spawned (or attached to) the backend
 *    on and hands it over through `shell.ts`, exactly as it hands over the auth
 *    token; this module composes with that, it does not replace it.
 *
 * Resolved once at startup (`initBackendOrigin`, before the first REST call or
 * socket — `App.tsx`), then read synchronously, mirroring `token.ts`.
 */

import { backendOrigin as shellBackendOrigin } from "./shell";

/**
 * `""` = same-origin: `"" + "/api/x"` is the relative URL the browser build has
 * always issued. A shell sets it to `http://127.0.0.1:<port>`. Never contains a
 * trailing slash, so concatenation with a leading-slash path is exact.
 */
let httpOrigin = "";

/**
 * Ask the host where the backend is. A no-op in a browser tab (the origin stays
 * `""`), so the browser path never changes. Never throws: `shell.ts` already
 * swallows its own IPC failure and returns `null`, and a null answer leaves the
 * origin same-origin — which is correct for a browser and the safe default for a
 * shell that could not answer (the relative URL simply fails at the call site,
 * the same outcome as a wrong origin, and the next launch asks again).
 */
export async function initBackendOrigin(): Promise<void> {
  const fromShell = await shellBackendOrigin();
  if (fromShell !== null && fromShell !== "") httpOrigin = fromShell;
}

/** The resolved backend HTTP origin, or `""` for same-origin. Exposed for the
 * seam's own tests and anyone that needs the raw prefix; call sites use
 * `apiUrl`/`wsUrl` instead. */
export function backendHttpOrigin(): string {
  return httpOrigin;
}

/** A REST path (`/api/…`) as a URL the current host can reach. */
export function apiUrl(path: string): string {
  return `${httpOrigin}${path}`;
}

/**
 * A WebSocket path (`/ws/…`) as a URL the current host can reach.
 *
 * Same-origin (browser): derive `ws(s)://` from the page's own protocol and
 * host, as before. Shell: swap the resolved origin's `http`→`ws` prefix — which
 * also carries `https`→`wss` should the asset host ever be TLS — and keep the
 * `127.0.0.1:<port>` authority so the socket reaches the Python backend, not the
 * asset protocol.
 */
export function wsUrl(path: string): string {
  if (httpOrigin === "") {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}${path}`;
  }
  return `${httpOrigin.replace(/^http/, "ws")}${path}`;
}
