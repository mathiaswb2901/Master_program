/**
 * Who owns a Monaco text model, and when it dies.
 *
 * The rule (`monaco.ts`, ROADMAP product principle 4): a pane is a **view** onto
 * a model it does not own, so a view going away never ends the model — only the
 * store closing the *file* does, and even that waits for the last view to let
 * go. `e2e/editorPanes.spec.ts` is the reproduction in a browser, on the real
 * editor; this is the same contract asserted on `monaco.ts` alone, in
 * milliseconds, so that a regression fails by name rather than as a blank pane
 * three journeys later.
 *
 * The bundle is hand-stubbed for the reason `monacoLoad.test.ts` gives: the unit
 * suite is node-only by design, and 3.3 MB of browser editor buys these
 * assertions nothing. What is faked is Monaco's model *registry* — a map from
 * Uri to model, which is all `monaco.ts` uses it as.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/** A text model with the one behaviour under test: it can be disposed, and it
 * refuses to be written to afterwards — exactly as Monaco's does. */
class FakeModel {
  disposed = false;
  constructor(
    private value: string,
    private readonly onDispose: () => void,
  ) {}
  getValue(): string {
    return this.value;
  }
  setValue(next: string): void {
    if (this.disposed) throw new Error("Model is disposed!");
    this.value = next;
  }
  dispose(): void {
    this.disposed = true;
    this.onDispose();
  }
}

/** An editor attached to a model. `saveViewState`/`restoreViewState` are
 * recorded rather than simulated: what matters is that *every* editor showing a
 * model is asked, not just one. */
class FakeEditor {
  restored: string[] = [];
  constructor(
    private readonly model: FakeModel,
    private readonly position: string,
  ) {}
  getModel(): FakeModel {
    return this.model;
  }
  saveViewState(): string {
    return this.position;
  }
  restoreViewState(state: string | null): void {
    if (state !== null) this.restored.push(state);
  }
}

const { store, editors } = vi.hoisted(() => ({
  store: new Map<string, unknown>(),
  editors: [] as unknown[],
}));

vi.mock("./monacoBundle", () => ({
  configureBundle: () => ({
    Uri: { parse: (value: string) => ({ key: value, toString: () => value }) },
    editor: {
      defineTheme: () => undefined,
      getModel: ({ key }: { key: string }) => store.get(key) ?? null,
      getEditors: () => editors,
    },
  }),
}));

beforeEach(() => {
  vi.resetModules(); // `monaco.ts` memoizes both the load and the registry
  store.clear();
  editors.length = 0;
  vi.stubGlobal("document", {
    documentElement: { getAttribute: () => null },
  });
  vi.stubGlobal("getComputedStyle", () => ({ getPropertyValue: () => "#1A1D22" }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** `monaco.ts` with Monaco loaded, plus a way to put models in the fake
 * registry the way `@monaco-editor/react` puts them in the real one. */
async function loaded(): Promise<{
  monaco: typeof import("./monaco");
  open: (path: string, value?: string, onWillDispose?: () => void) => FakeModel;
}> {
  const monaco = await import("./monaco");
  await monaco.loadMonaco();
  const open = (path: string, value = "", onWillDispose = (): void => undefined): FakeModel => {
    const key = monaco.editorPathProp(path);
    const model = new FakeModel(value, () => {
      onWillDispose();
      store.delete(key);
    });
    store.set(key, model);
    return model;
  };
  return { monaco, open };
}

describe("the model registry", () => {
  it("keeps the model when one of two views lets go", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py", "PRICE = 1\n");

    // The tab strip and an `editors#src/bid.py` pane, both showing it.
    monaco.acquireModel("src/bid.py");
    monaco.acquireModel("src/bid.py");
    monaco.releaseModel("src/bid.py"); // the pane closes

    expect(model.disposed, "the surviving pane's buffer").toBe(false);
    // …and it is still writable, which is what the blank pane was not.
    expect(monaco.setModelContent("src/bid.py", "PRICE = 2\n")).toBe("PRICE = 2\n");
  });

  it("keeps it even when the last view lets go, while the file is still open", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py");

    monaco.acquireModel("src/bid.py");
    monaco.releaseModel("src/bid.py");

    // Zero views is not zero owners: the file is open in the tab strip, which
    // is simply showing another tab. Disposing here would throw away the undo
    // history of a file the user still has open.
    expect(model.disposed).toBe(false);
  });

  it("disposes it when the store closes the file and the last view has gone", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py");

    monaco.acquireModel("src/bid.py");
    monaco.acquireModel("src/bid.py");
    monaco.disposeModel("src/bid.py"); // the tab closes while two panes show it

    expect(model.disposed, "never under a live editor").toBe(false);
    monaco.releaseModel("src/bid.py");
    expect(model.disposed).toBe(false);
    monaco.releaseModel("src/bid.py");
    expect(model.disposed, "the last view let go").toBe(true);
  });

  it("disposes it at once when the file closes and nothing is showing it", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py");

    monaco.acquireModel("src/bid.py");
    monaco.releaseModel("src/bid.py");
    monaco.disposeModel("src/bid.py");

    expect(model.disposed).toBe(true);
  });

  it("un-retires a model a pane opens again", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py");

    monaco.acquireModel("src/bid.py");
    monaco.disposeModel("src/bid.py"); // retired, one view left
    monaco.acquireModel("src/bid.py"); // …and reopened before that view went
    monaco.releaseModel("src/bid.py");

    expect(model.disposed).toBe(false);
  });

  it("survives a release with no acquire, and a dispose with no model", async () => {
    const { monaco } = await loaded();
    expect(() => {
      monaco.releaseModel("never/opened.py");
      monaco.disposeModel("never/opened.py");
    }).not.toThrow();
  });

  it("is one model per file however the path is spelled", async () => {
    const { monaco } = await loaded();
    // Two buffers over one file, each able to save over the other, is the worst
    // outcome available here — so the separator is normalised into the key.
    expect(monaco.editorPathProp("src\\bid.py")).toBe(monaco.editorPathProp("src/bid.py"));
  });
});

describe("an external change", () => {
  it("puts every view back where it was looking, not just one", async () => {
    const { monaco, open } = await loaded();
    const model = open("src/bid.py", "old\n");
    const strip = new FakeEditor(model, "strip-position");
    const split = new FakeEditor(model, "split-position");
    const elsewhere = new FakeEditor(new FakeModel("", () => undefined), "other-file");
    editors.push(strip, split, elsewhere);

    expect(monaco.setModelContent("src/bid.py", "new\n")).toBe("new\n");

    // `setValue` is a full-model edit: a pane nobody restored jumps to line 1.
    expect(strip.restored).toEqual(["strip-position"]);
    expect(split.restored).toEqual(["split-position"]);
    // …and an editor on another file is not touched.
    expect(elsewhere.restored).toEqual([]);
  });

  it("says so when there is no model for the path yet", async () => {
    const { monaco } = await loaded();
    // Which is also the answer before Monaco has loaded at all — the store
    // handles both with `setModelContent(...) ?? content`.
    expect(monaco.setModelContent("src/bid.py", "new\n")).toBeNull();
  });
});

describe("view state", () => {
  it("belongs to the pane, not to the file", async () => {
    const { monaco } = await loaded();
    const strip = { position: "top" } as unknown as Parameters<
      typeof monaco.rememberViewState
    >[2];
    const split = { position: "line 400" } as unknown as typeof strip;

    monaco.rememberViewState("editors", "src/bid.py", strip);
    monaco.rememberViewState("editors#src/bid.py", "src/bid.py", split);

    // Two panes on one file remember two different places.
    expect(monaco.recallViewState("editors", "src/bid.py")).toBe(strip);
    expect(monaco.recallViewState("editors#src/bid.py", "src/bid.py")).toBe(split);
    expect(monaco.recallViewState("editors#other", "src/bid.py")).toBeNull();
  });

  it("goes away with the model it described", async () => {
    const { monaco, open } = await loaded();
    open("src/bid.py");
    const state = {} as unknown as Parameters<typeof monaco.rememberViewState>[2];
    monaco.rememberViewState("editors", "src/bid.py", state);

    monaco.disposeModel("src/bid.py");

    // Reopening reloads from disk, so a remembered scroll position into a
    // buffer that no longer exists would be a lie about a different file.
    expect(monaco.recallViewState("editors", "src/bid.py")).toBeNull();
  });

  it("goes away even when the disposal itself files one more", async () => {
    const { monaco, open } = await loaded();
    const state = {} as unknown as Parameters<typeof monaco.rememberViewState>[2];
    // What really happens on disposal: Monaco calls `setModel(null)` on every
    // editor still attached, `CodeEditor`'s `onWillChangeModel` fires, and it
    // saves one last view state — *during* the dispose. Clearing before that
    // would leave the entry behind, keyed to a buffer that no longer exists.
    open("src/bid.py", "", () => {
      monaco.rememberViewState("editors", "src/bid.py", state);
    });

    monaco.disposeModel("src/bid.py");

    expect(monaco.recallViewState("editors", "src/bid.py")).toBeNull();
  });
});
