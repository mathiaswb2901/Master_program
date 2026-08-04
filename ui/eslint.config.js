import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";

/** Minimal flat config: the recommended sets plus the underscore convention the
 * codebase already uses for deliberately unused bindings (`_props`). */
export default tseslint.config(
  { ignores: ["dist"] },
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
);
