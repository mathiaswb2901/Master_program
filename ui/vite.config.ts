import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Only global used here; @types/node would be a dependency for one line.
declare const process: { env: Record<string, string | undefined> };

/** Backend the dev server and `vite preview` proxy to. The Playwright suite
 * runs its own backend on another port and sets this (playwright.config.ts). */
const backend = process.env.WORKBENCH_SERVER_URL ?? "http://127.0.0.1:8787";

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
