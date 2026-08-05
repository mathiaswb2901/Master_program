import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

/** Minimal flat config: the recommended sets plus the underscore convention the
 * codebase already uses for deliberately unused bindings (`_props`). */
export default tseslint.config(
  { ignores: ["dist", "playwright-report", "playwright-report-perf", "perf-results", "test-results"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: { globals: globals.browser },
    rules: {
      "@typescript-eslint/no-unused-vars": [
        "error",
        {
          argsIgnorePattern: "^_",
          varsIgnorePattern: "^_",
          caughtErrorsIgnorePattern: "^_",
        },
      ],
    },
  },
  {
    // The Playwright harness runs in node, not the browser. It is outside the
    // `tsc -b` program (tsconfig.json), so lint is the only checker it gets.
    files: ["e2e/**/*.ts", "playwright.config.ts", "playwright.perf.config.ts"],
    languageOptions: { globals: globals.node },
  },
);
