/**
 * The file tree's virtualiser does arithmetic on two numbers that live in CSS.
 *
 * `FileTree.tsx` positions row *n* at `n * ROW_HEIGHT` inside a spacer of
 * `rows * ROW_HEIGHT`, and derives its window from the scroll position minus
 * the scroller's own top padding. Both numbers are design tokens
 * (`--row-height`, `--space-2`), so a token edit that never touches TypeScript
 * would silently misalign every row below the fold — rows overlapping, the last
 * ones unreachable, the scrollbar lying about how much is there.
 *
 * Nothing else in the suite can see that: the Playwright budget counts *how
 * many* rows are mounted, not where they are, and a unit test in `src/` cannot
 * read a stylesheet (vitest hands CSS imports back empty). So the check lives
 * here, next to the budgets it protects, and costs a millisecond instead of a
 * full perf run — the same reason `workspace.test.ts` is in this directory.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const UI_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..");

const read = (relative: string): string =>
  fs.readFileSync(path.join(UI_ROOT, ...relative.split("/")), "utf-8");

describe("file tree row geometry", () => {
  const tokens = read("src/design/tokens.css");
  const panel = read("src/panels/FileTree.tsx");
  const css = read("src/styles/filetree.css");

  it("keeps ROW_HEIGHT equal to the --row-height token", () => {
    expect(tokens).toMatch(/--row-height:\s*26px/);
    expect(panel).toMatch(/const ROW_HEIGHT = 26;/);
  });

  it("keeps LIST_PAD equal to the scroller's own padding", () => {
    expect(tokens).toMatch(/--space-2:\s*4px/);
    expect(css).toMatch(/\.wb-filetree \{[^}]*padding: var\(--space-2\) 0;/s);
    expect(panel).toMatch(/const LIST_PAD = 4;/);
  });

  it("positions rows absolutely inside a relative spacer", () => {
    // The other half of the arithmetic: rows are placed by `top`, so they must
    // be taken out of flow, and the spacer must be their containing block.
    expect(css).toMatch(/\.wb-tree-viewport \{[^}]*position: relative;/s);
    expect(css).toMatch(/\.wb-tree-row \{[^}]*position: absolute;/s);
  });
});
