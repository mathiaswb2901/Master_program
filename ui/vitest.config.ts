import { defineConfig } from "vitest/config";

/** Unit tests only, node environment: the modules under test are deliberately
 * pure (chords, markdown, path resolution, the tool registry's derivations), so
 * no DOM stack is needed — `src/test-setup.ts` covers the single exception. */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
    setupFiles: ["./src/test-setup.ts"],
  },
});
