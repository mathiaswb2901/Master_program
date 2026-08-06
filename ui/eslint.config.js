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
  {
    // The perf lane is one worker against one workspace, and the app persists
    // the window arrangement *into* that workspace — so a spec that takes
    // Playwright's own `test` starts in whatever window the spec before it left,
    // and its budget silently becomes a function of the alphabet. `./window`
    // hands back the same `test` with the arrangement reset first, and this is
    // what makes that the only way to get one. (`e2e/perf/window.ts`.)
    files: ["e2e/perf/**/*.spec.ts"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          paths: [
            {
              name: "@playwright/test",
              importNames: ["test"],
              message:
                "Perf specs take `test` from ./window, which resets the saved arrangement " +
                "first — a budget that depends on which spec ran before it is not a budget.",
            },
          ],
        },
      ],
    },
  },
);
