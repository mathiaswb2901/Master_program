import { defineConfig } from "vitest/config";

/** Unit tests only, node environment: the modules under test are deliberately
 * pure (chords, markdown, path resolution), so no DOM stack is needed. */
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
