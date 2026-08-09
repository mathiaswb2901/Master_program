/**
 * What the notebook pane paints for an output — and, for the one payload the
 * server may refuse to carry, what it paints *instead*.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design —
 * `vitest.config.ts`), so the two neighbours this panel reaches through are
 * stubbed: `./Panes` pulls in dockview's runtime and, through the registry,
 * Monaco; `../store` reads `document` at import. The module under test is the
 * real one, and so is the notebook store it renders from.
 *
 * The claim worth pinning here is the byte one. A notebook can legally hold a
 * 35 MB base64 image — inside the server's file ceiling, so nothing upstream
 * refuses it — and an `<img src="data:...">` of that size is a tab that stops
 * responding while the main thread decodes it. The server omits it; this asserts
 * the panel then renders a stated absence with the size, and **no `<img>` at
 * all**, which is the half a server test cannot make.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { NotebookDocument, NotebookOutput } from "../types";

vi.mock("./Panes", () => ({ revealPane: () => undefined }));
vi.mock("../store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({ openFile: () => undefined, setActiveFile: () => undefined }),
  }),
}));

const { NotebookBody } = await import("./Notebook");

const PATH = "se3/spread.ipynb";

const output = (over: Partial<NotebookOutput>): NotebookOutput => ({
  output_type: "display_data",
  mime: "",
  stream: null,
  text: null,
  html_source: null,
  image: null,
  image_omitted_bytes: null,
  ename: null,
  evalue: null,
  truncated: false,
  ...over,
});

const docWith = (outputs: NotebookOutput[]): NotebookDocument => ({
  path: PATH,
  nbformat: 4,
  nbformat_minor: 4,
  language: "python",
  kernel: "Python 3 (ipykernel)",
  cells: [
    {
      id: "c0",
      cell_type: "code",
      source: "spread.plot()",
      execution_count: 2,
      outputs,
      truncated: false,
    },
  ],
  cell_count: 1,
  truncated: false,
  valid: true,
  validation_message: null,
});

/** One code cell's outputs, rendered.
 *
 * `NotebookBody` rather than `NotebookView`: the view reads its document out of
 * the notebook store through `useSyncExternalStore`, whose *server* snapshot is
 * the store's initial state — so a seeded store is invisible to a static render
 * and every assertion below would pass against the loading card instead. The
 * document is handed over directly, which is what is under test anyway. */
const paint = (outputs: NotebookOutput[]): string =>
  renderToStaticMarkup(<NotebookBody doc={docWith(outputs)} path={PATH} />);

const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" +
  "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

describe("an image the server carried", () => {
  it("paints it from its own bytes, never from a URL", () => {
    const html = paint([
      output({ mime: "image/png", image: { mime: "image/png", base64: PNG_BASE64 } }),
    ]);
    expect(html).toContain(`src="data:image/png;base64,${PNG_BASE64}"`);
    expect(html).not.toContain("wb-nb-out is-image-cut");
  });
});

describe("an image the server would not carry", () => {
  const omitted = output({
    mime: "image/png",
    image_omitted_bytes: 36_909_875,
    truncated: true,
  });

  it("renders no <img> at all", () => {
    // The whole point. A placeholder that still emitted an `<img>` — even one
    // with an empty `src` — would be the same unbounded paint by another name.
    const html = paint([omitted]);
    expect(html).not.toContain("<img");
    expect(html).not.toContain("data:image");
  });

  it("says an image is missing, which kind, and how big", () => {
    const html = paint([omitted]);
    expect(html).toContain("Image not shown");
    expect(html).toContain("image/png");
    // Powers of 1024, the way the user's own file manager names them.
    expect(html).toContain("35.2 MB");
    expect(html).toContain("it is in the file");
  });

  it("does not also claim the output was empty", () => {
    // The generic branch below it renders "Empty output"; a figure that was too
    // big to send is the opposite of a cell that produced nothing.
    expect(paint([omitted])).not.toContain("Empty output");
  });
});
