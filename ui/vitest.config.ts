import { defineConfig } from "vitest/config";

/** Unit tests only, node environment: the modules under test are deliberately
 * pure (chords, markdown, path resolution, the tool registry's derivations), so
 * no DOM stack is needed — `src/test-setup.ts` covers the single exception.
 *
 * `e2e/**` is Playwright's, with one exception: the harness modules that decide
 * things rather than drive a browser — which directory a run owns, and therefore
 * which one it may delete. That decision is worth a test that costs milliseconds
 * instead of a full perf run, so `*.test.ts` under `e2e/` runs here while
 * `*.spec.ts` stays Playwright's alone. */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}", "e2e/**/*.test.ts"],
    setupFiles: ["./src/test-setup.ts"],
  },
});
