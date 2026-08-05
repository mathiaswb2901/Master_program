import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Only global used here; @types/node would be a dependency for one line.
declare const process: { env: Record<string, string | undefined> };

/** Backend the dev server and `vite preview` proxy to.
 *
 * `WORKBENCH_SERVER_URL` wins — the Playwright suite runs its own backend on
 * another port and sets it (playwright.config.ts). Without it we fall back to
 * the server's *own* settings, `WORKBENCH_HOST`/`WORKBENCH_PORT`, which are also
 * what the desktop shell probes and spawns with (`desktop/src-tauri/src/backend.rs`).
 * One authority for the pair is the whole point: `WORKBENCH_PORT=9000 npm run
 * tauri dev` used to give you a live backend on 9000 and a page proxying every
 * REST call and WebSocket to 8787 — a dead UI under a log line saying the
 * backend was ready.
 */
const host = process.env.WORKBENCH_HOST || "127.0.0.1";
const port = process.env.WORKBENCH_PORT || "8787";
// A server bound to the wildcard is still reached over loopback from here.
const loopback = host === "0.0.0.0" || host === "::" ? "127.0.0.1" : host;
const backend = process.env.WORKBENCH_SERVER_URL ?? `http://${loopback}:${port}`;

const proxy = {
  "/api": { target: backend },
  "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
};

export default defineConfig({
  plugins: [react()],
  server: { proxy },
  // `vite preview` serves the built bundle; it needs the same proxy so the E2E
  // suite (and anyone eyeballing a production build) talks to a live backend.
  preview: { proxy },
});
