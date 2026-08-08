/**
 * The Search surface: results grouped by file, the honest "none", and the
 * click-to-open wiring.
 *
 * Node-only, like the other panel tests (`ReviewPanel.test.tsx`): the pure
 * components take their data as props, so `renderToStaticMarkup` renders them to
 * a string with no DOM, and a click is exercised by invoking the element's own
 * `onClick` prop directly — which is what proves a hit is wired to `openHit`
 * without a browser. The browser half (Ctrl+Shift+F, a real query, a hit opening
 * the editor at its line) is `ui/e2e/search.spec.ts`.
 *
 * `../search` is mocked so the wiring can be observed and so Monaco/the store are
 * never pulled in; `../dock` is mocked because the descriptor's command reaches
 * the whole registry, none of which is exercised here.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { FileMatches, SearchResponse } from "../types";

const openHit = vi.fn();
vi.mock("../search", () => ({ openHit: (...args: unknown[]) => openHit(...args) }));
vi.mock("../dock", () => ({ openPanel: vi.fn() }));

const { HitRow, FileGroup, SearchResults, searchTool } = await import("./Search");

const hit = (line: number, col: number, text: string) => ({
  line,
  col,
  text,
  line_truncated: false,
});

const twoFiles: SearchResponse = {
  query: "SE3",
  files: [
    { path: "notes.md", hits: [hit(3, 0, "SE3 battery notes")] },
    { path: "src/model.py", hits: [hit(1, 14, "PRICE_AREA = 'SE3'"), hit(9, 4, "    SE3 again")] },
  ],
  total_hits: 3,
  files_with_matches: 2,
  truncated: false,
};

describe("SearchResults", () => {
  it("groups hits under their files, with line numbers and the matched span marked", () => {
    const html = renderToStaticMarkup(<SearchResults results={twoFiles} />);
    expect(html).toContain("notes.md");
    expect(html).toContain("src/model.py");
    // The summary states the totals.
    expect(html).toContain("3 matches in 2 files");
    // The match text is present, and the matched span is wrapped in <mark>.
    expect(html).toContain("PRICE_AREA = &#x27;");
    expect(html).toContain('<mark class="wb-search-mark">SE3</mark>');
    // No truncation note when the result is whole.
    expect(html).not.toContain("Narrow the query");
  });

  it("says 'no matches' rather than showing blankness (AXI shape 2)", () => {
    const none: SearchResponse = {
      query: "absent",
      files: [],
      total_hits: 0,
      files_with_matches: 0,
      truncated: false,
    };
    const html = renderToStaticMarkup(<SearchResults results={none} />);
    expect(html).toContain("No matches for");
    expect(html).toContain("absent");
  });

  it("names the cut when the result was truncated (AXI shape 1)", () => {
    const html = renderToStaticMarkup(<SearchResults results={{ ...twoFiles, truncated: true }} />);
    expect(html).toContain("Narrow the query");
  });
});

describe("click-to-open wiring", () => {
  it("a hit row opens its file at its line", () => {
    openHit.mockClear();
    const element = HitRow({ path: "src/model.py", hit: hit(9, 4, "    SE3 again"), query: "SE3" });
    // Invoke the button's own onClick — the wiring, without a DOM.
    (element.props as { onClick: () => void }).onClick();
    expect(openHit).toHaveBeenCalledWith("src/model.py", 9);
  });

  it("a file header opens the file at its first hit", () => {
    openHit.mockClear();
    const file: FileMatches = { path: "notes.md", hits: [hit(3, 0, "SE3"), hit(7, 0, "SE3")] };
    const element = FileGroup({ file, query: "SE3" });
    // The <li>'s first child is the header button; invoke its onClick.
    const head = (element.props as { children: Array<{ props: { onClick: () => void } }> })
      .children[0];
    head.props.onClick();
    expect(openHit).toHaveBeenCalledWith("notes.md", 3);
  });
});

describe("the descriptor", () => {
  it("registers Ctrl+Shift+F on its open command", () => {
    expect(searchTool.id).toBe("search");
    expect(searchTool.shortcuts?.["search.open"]).toEqual(["Ctrl+Shift+F"]);
    // Singular and opened on demand — see the module docstring.
    expect(searchTool.panel?.singleton).toBe(true);
    expect(searchTool.panel?.openByDefault).toBe(false);
  });
});
