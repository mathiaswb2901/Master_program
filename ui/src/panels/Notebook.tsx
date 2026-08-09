/**
 * The notebook view — a `.ipynb` read as a first-class document (ROADMAP M4,
 * the half PR #62 deferred).
 *
 * ## Reading, not running
 *
 * There is no run button here, and there is nothing behind one: no kernel, no
 * execution endpoint, no write path (`services/notebook.py`). That is the
 * product decision, not a staging note — an affordance that does nothing is a
 * lie about what the app can do, and the honest UI for a capability we do not
 * have is its **absence**. What a code cell shows is history: the `[7]` it last
 * ran as, and what it produced then.
 *
 * ## Two ways in, one renderer
 *
 * - **A document view.** `.ipynb` is its own `OpenFile` kind, so clicking a
 *   notebook in the tree opens the notebook rather than raw JSON — which is
 *   ROADMAP item 16's exit line, and the clause PR #62 could not close.
 * - **A plural pane.** `notebook#<workspace-relative path>` puts one notebook
 *   in a pane of its own, beside the editor or beside another notebook. The
 *   pane id is the whole of what `.workbench/layouts.json` persists
 *   (`../panes.ts`), so a saved arrangement comes back on the same two files.
 *
 * Both render `NotebookView`. There is no second render path, so there is no
 * second place for the safety posture below to be got wrong.
 *
 * ## A restored pane is vetted before it is believed
 *
 * Layouts persist; files do not. Every pane re-reads its notebook on mount and
 * three answers are distinguished, because the recoveries differ:
 *
 * - **gone** (404) — a named tombstone with **Reopen**, which re-reads. A file
 *   comes back: a branch switch, a `git checkout`, a sync that finished;
 * - **failed** (422 and every other refusal) — a named degraded card carrying
 *   the server's own reason, and **Open as JSON**, which opens the raw file in
 *   the editor. There is a file to look at, so the card hands it over rather
 *   than being a dead end;
 * - **ready** — even when the schema complained (`doc.valid`), which is said in
 *   a notice above the cells rather than by refusing to render them.
 *
 * Neither is ever a blank pane.
 *
 * ## Nothing crosses as markup
 *
 * Markdown cells go through the app's own renderer (`../markdown.tsx`), which
 * builds React elements and never touches `dangerouslySetInnerHTML`. A
 * notebook's `text/html` output is arbitrary author-controlled markup, so it is
 * rendered **as source text** in a labelled block — the server carries it in a
 * field called `html_source` for exactly this reason. Images are `data:` URIs
 * built from base64 the server already had, so no output can turn into a
 * network request. Grep this file for `dangerouslySetInnerHTML`: there is none.
 */

import type { DockviewPanelApi, IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import { Markdown } from "../markdown";
import {
  countPhrase,
  executionLabel,
  isNotebookPath,
  notebookTitle,
  tokenizeCode,
  useNotebookStore,
  watchNotebooks,
  type NotebookEntry,
} from "../notebook";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import { useStore, type OpenFile } from "../store";
import type { NotebookCell, NotebookDocument, NotebookOutput, TreeNode } from "../types";

import { revealPane } from "./Panes";

import "../styles/notebook.css";

function NotebookIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4.5 1.5h8a.5.5 0 0 1 .5.5v12a.5.5 0 0 1-.5.5h-8a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5Z" />
      <path d="M3 4h3M3 8h3M3 12h3" />
      <path d="M7 5.5h3.5M7 9.5h3.5" />
    </svg>
  );
}

// ---- cells --------------------------------------------------------------------

/** Code, coloured by the pure token pass in `../notebook.ts` — spans of text
 * nodes, never markup. The syntax colours are the ANSI palette Monaco's own
 * theme is built from (DESIGN.md §2.7/§2.8), so a code cell reads like the
 * editor rather than like a second idea about what a keyword looks like. */
function CodeSource({ source, language }: { source: string; language: string }) {
  return (
    <pre className="wb-nb-code">
      <code>
        {tokenizeCode(source, language).map((token, index) =>
          token.kind === "plain" ? (
            token.text
          ) : (
            <span key={index} className={`wb-nb-t-${token.kind}`}>
              {token.text}
            </span>
          ),
        )}
      </code>
    </pre>
  );
}

/** One truncation notice, in the one phrasing every cap uses. */
function CutNotice({ what }: { what: string }) {
  return (
    <p className="wb-nb-cut" role="note">
      {what} — the rest is in the file.
    </p>
  );
}

function Output({ output }: { output: NotebookOutput }) {
  const { image, html_source: html, text } = output;

  if (output.output_type === "error") {
    return (
      <div className="wb-nb-out is-error">
        {output.ename !== null && (
          <p className="wb-nb-err-head">
            {output.ename}
            {output.evalue === null ? "" : `: ${output.evalue}`}
          </p>
        )}
        {text !== null && text !== "" && <pre className="wb-nb-out-text">{text}</pre>}
        {output.truncated && <CutNotice what="Traceback cut" />}
      </div>
    );
  }

  if (image !== null) {
    return (
      <div className="wb-nb-out is-image">
        {/* A `data:` URI built from base64 the server already held: the bytes
            never become a URL anything could be asked to fetch. */}
        <img src={`data:${image.mime};base64,${image.base64}`} alt="Cell output" />
      </div>
    );
  }

  if (html !== null) {
    return (
      <div className="wb-nb-out is-html">
        <p className="wb-nb-out-label">HTML output, shown as source</p>
        <pre className="wb-nb-out-text">{html}</pre>
        {output.truncated && <CutNotice what="HTML cut" />}
      </div>
    );
  }

  const stream = output.stream === "stderr" ? " is-stderr" : "";
  return (
    <div className={`wb-nb-out${stream}`}>
      {text === null || text === "" ? (
        <p className="wb-nb-out-label">Empty output</p>
      ) : (
        <pre className="wb-nb-out-text">{text}</pre>
      )}
      {output.truncated && <CutNotice what="Output cut" />}
    </div>
  );
}

function Cell({ cell, language }: { cell: NotebookCell; language: string }) {
  if (cell.cell_type === "markdown") {
    return (
      <div className="wb-nb-cell is-markdown" data-cell-type="markdown">
        <Markdown text={cell.source} />
        {cell.truncated && <CutNotice what={`Cell cut at ${countPhrase(cell.source.length, "character")}`} />}
      </div>
    );
  }

  if (cell.cell_type === "raw") {
    return (
      <div className="wb-nb-cell is-raw" data-cell-type="raw">
        <pre className="wb-nb-raw">{cell.source}</pre>
        {cell.truncated && <CutNotice what={`Cell cut at ${countPhrase(cell.source.length, "character")}`} />}
      </div>
    );
  }

  return (
    <div className="wb-nb-cell is-code" data-cell-type="code">
      <div className="wb-nb-code-row">
        {/* History, not a control: the count this cell last ran as. `[ ]` means
            it has never run, which is a different fact from "ran and printed
            nothing" and is worth the two characters. */}
        <span className="wb-nb-count u-tabular" title="Execution count when this notebook was saved">
          {executionLabel(cell.execution_count)}
        </span>
        <CodeSource source={cell.source} language={language} />
      </div>
      {cell.truncated && <CutNotice what={`Cell cut at ${countPhrase(cell.source.length, "character")}`} />}
      {cell.outputs.length > 0 && (
        <div className="wb-nb-outs">
          {cell.outputs.map((output, index) => (
            <Output key={index} output={output} />
          ))}
        </div>
      )}
    </div>
  );
}

// ---- the document -------------------------------------------------------------

function NotebookBody({ doc, path }: { doc: NotebookDocument; path: string }) {
  return (
    <>
      <header className="wb-nb-head">
        <span className="wb-nb-name u-truncate" title={path}>
          {notebookTitle(path)}
        </span>
        <span className="wb-nb-meta u-truncate">
          {doc.kernel ?? doc.language} · {countPhrase(doc.cell_count, "cell")}
        </span>
        {/* Said out loud, once, where the document is named. The absence of a
            run control is the other half of the same sentence. */}
        <span className="wb-nb-readonly">Read-only</span>
        <button
          type="button"
          className="wb-btn wb-btn-sm wb-btn-ghost"
          onClick={() => void useNotebookStore.getState().load(path, true)}
        >
          Reload
        </button>
      </header>
      <div className="wb-nb-scroll">
        {!doc.valid && (
          <p className="wb-nb-notice is-warn" role="status">
            This notebook does not match the schema
            {doc.validation_message === null ? "" : `: ${doc.validation_message}`}. Showing it
            anyway.
          </p>
        )}
        {doc.truncated && (
          <p className="wb-nb-notice" role="status">
            Showing the first {countPhrase(doc.cells.length, "cell")} of{" "}
            {doc.cell_count.toLocaleString("en-US")}.
          </p>
        )}
        {doc.cells.length === 0 ? (
          <div className="wb-empty">
            <div className="wb-empty-title">Nothing in this notebook</div>
            <div className="wb-empty-hint">It has no cells yet.</div>
          </div>
        ) : (
          doc.cells.map((cell) => <Cell key={cell.id} cell={cell} language={doc.language} />)
        )}
      </div>
    </>
  );
}

/**
 * The one renderer both entry points use, with every state it can be in.
 *
 * `load` is idempotent per path and shared across panes, so a notebook open in
 * two panes is fetched once and both show the same document.
 */
export function NotebookView({ path }: { path: string }) {
  const entry: NotebookEntry | undefined = useNotebookStore((s) => s.notebooks[path]);

  useEffect(() => {
    watchNotebooks();
    void useNotebookStore.getState().load(path);
  }, [path]);

  if (entry === undefined || (entry.status === "loading" && entry.doc === null)) {
    return (
      <div className="wb-nb">
        <div className="wb-nb-state">Reading {notebookTitle(path)}…</div>
      </div>
    );
  }

  if (entry.status === "gone") {
    // The tombstone. A pane restored from a layout names a file the workspace
    // may not have any more — and a file can come back, so the one action is
    // the one that would notice.
    return (
      <div className="wb-nb">
        <div className="wb-nb-state is-empty">
          <p className="wb-nb-state-title">{notebookTitle(path)} is not in this workspace</p>
          <p className="wb-nb-state-detail u-truncate" title={path}>
            {path}
          </p>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => void useNotebookStore.getState().load(path, true)}
          >
            Reopen
          </button>
        </div>
      </div>
    );
  }

  if (entry.status === "failed" || entry.doc === null) {
    // The degraded card: what failed, and the file itself. `entry.error` is the
    // server's sentence, rendered rather than re-worded — a viewer that
    // paraphrases a parser is a viewer that will one day say the wrong thing.
    return (
      <div className="wb-nb">
        <div className="wb-nb-state is-failed" role="alert">
          <p className="wb-nb-state-title">Cannot read {notebookTitle(path)}</p>
          <p className="wb-nb-state-detail">{entry.error ?? "The notebook could not be read."}</p>
          <div className="wb-nb-state-actions">
            <button
              type="button"
              className="wb-btn wb-btn-sm wb-btn-outline"
              onClick={() => {
                // The escape hatch: the raw file in the editor, where it can be
                // read and repaired. Nothing else in the app can show it, since
                // `.ipynb` now opens here.
                void useStore.getState().openFile(path, true);
                // `revealPane(…, null)`, never `openPanel`: the Editor is a
                // plural tool, and `openPanel` mints a *second* pane when its
                // default one is already open (docs/tools.md). That pane would
                // be an instance bound to a throwaway key — a file pane pointed
                // at nothing, which then takes the active file with it and the
                // raw JSON never comes forward. Measured, in this journey.
                revealPane("editors", null);
              }}
            >
              Open as JSON
            </button>
            <button
              type="button"
              className="wb-btn wb-btn-sm wb-btn-ghost"
              onClick={() => void useNotebookStore.getState().load(path, true)}
            >
              Try again
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="wb-nb">
      <NotebookBody doc={entry.doc} path={path} />
    </div>
  );
}

// ---- the two entry points ------------------------------------------------------

/** The document view: one open `.ipynb` inside the editor area. */
function NotebookDocumentView({ file }: { file: OpenFile }) {
  return <NotebookView path={file.path} />;
}

/**
 * The pane. `notebook#<path>` shows that notebook; the bare `notebook` pane is
 * the one a user can reach by splitting and picking the tool itself, and it
 * says how to point it at something rather than rendering nothing.
 */
export function NotebookPanel(props: IDockviewPanelProps) {
  const path = paneInstance(props.api.id);
  return path === null ? <NotebookIndex /> : <NotebookPane path={path} api={props.api} />;
}

function NotebookIndex() {
  return (
    <div className="wb-nb">
      <div className="wb-empty">
        <div className="wb-empty-title">No notebook here</div>
        <div className="wb-empty-hint">
          Open one — <span className="wb-keycap">Ctrl</span>{" "}
          <span className="wb-keycap">Shift</span> <span className="wb-keycap">P</span>, then
          &ldquo;Open notebook…&rdquo;
        </div>
      </div>
    </div>
  );
}

function NotebookPane({ path, api }: { path: string; api: DockviewPanelApi }) {
  // Ctrl+S saves the file you are looking at, and the focused pane decides
  // which one that is — the rule every pane in the window follows. A notebook
  // is read-only, so what this really buys is that focusing one does *not*
  // leave some other pane's file as the save target.
  useEffect(() => {
    if (api.isActive) useStore.getState().setActiveFile(path);
    const subscription = api.onDidActiveChange((event) => {
      if (event.isActive) useStore.getState().setActiveFile(path);
    });
    return () => subscription.dispose();
  }, [api, path]);

  return <NotebookView path={path} />;
}

// ---- opening one ---------------------------------------------------------------

/** Every `.ipynb` in the workspace, from the search index the QuickBar uses. */
function notebookPaths(tree: TreeNode | null): string[] {
  if (tree === null) return [];
  const out: string[] = [];
  const walk = (node: TreeNode): void => {
    if (node.kind === "file") {
      if (isNotebookPath(node.path)) out.push(node.path);
    } else for (const child of node.children ?? []) walk(child);
  };
  walk(tree);
  return out.sort((a, b) => a.localeCompare(b));
}

/**
 * "Open notebook…": pick one of the workspace's notebooks, and get a pane on it.
 *
 * The index is the same full walk the QuickBar's file search uses, fetched on
 * demand — a notebook list is not worth a request at launch. Rows are built
 * from a store read inside `rows()`, so the picker fills in when the walk lands
 * (the QuickBar re-renders on every keystroke and on store changes).
 */
function openNotebookFlow(): void {
  void useStore.getState().ensureFileIndex();
  // Opened *after* this turn, not inline. A QuickBar row runs its action and
  // then closes the palette synchronously (`QuickBar.tsx`), and closing clears
  // any pick — so a pick opened here would be wiped a microtask later and this
  // command would appear to do nothing. `documents.ts` is the shipped
  // precedent, and `documents.test.ts` pins the deferral. Harmless on the paths
  // with no palette open: one turn's delay before the picker appears.
  setTimeout(() => {
    useStore.getState().openQuickPick({
      label: "Open notebook",
      placeholder: "Which notebook?",
      rows: () => {
        const paths = notebookPaths(useStore.getState().tree);
        if (paths.length === 0) {
          // "None" said explicitly, never blankness: a picker with no rows
          // reads as broken, and "still loading" and "there are none" are
          // different answers a user has to be able to tell apart.
          return [
            {
              key: "none",
              title:
                useStore.getState().tree === null
                  ? "Looking for notebooks…"
                  : "No .ipynb files in this workspace",
              detail: "Create one with New document… → Notebook",
              disabled: true,
              run: () => undefined,
            },
          ];
        }
        return paths.map((path) => ({
          key: path,
          title: notebookTitle(path),
          detail: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "",
          category: "Notebooks",
          run: () => revealPane("notebook", path),
        }));
      },
    });
  }, 0);
}

// ---- registration ---------------------------------------------------------------

export const notebookTool: WorkbenchTool = {
  id: "notebook",
  title: "Notebook",
  icon: NotebookIcon,
  panel: {
    component: NotebookPanel,
    // Beside the editor, because a notebook is read *next to* the code being
    // written from it. Opened on demand only — nothing about a fresh workspace
    // says the user has notebooks.
    defaultLocation: { area: "center" },
    openByDefault: false,
    // Plural, and not as a feature request: two notebooks side by side is the
    // second thing anyone does with them, and the first is a notebook beside
    // the file it produced. The instance key is the workspace-relative path,
    // which is what makes a saved layout come back on the same notebooks.
    singleton: false,
    instances: {
      // The notebooks this window has read, not every `.ipynb` in the
      // workspace: the picker is a list of things already on your mind, and
      // "every file" is what the QuickBar's own search is for. `Open notebook…`
      // is the command that walks the workspace.
      options: () =>
        Object.keys(useNotebookStore.getState().notebooks)
          .sort((a, b) => a.localeCompare(b))
          .map((path) => ({
            id: `notebook.${path}`,
            title: notebookTitle(path),
            detail: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "",
            category: "Notebooks",
            key: () => path,
          })),
      titleFor: (key) => notebookTitle(key),
    },
  },
  documentView: {
    kind: "notebook",
    component: NotebookDocumentView,
    hostClassName: "wb-notebook-host",
    // Drawn by us, in the window's own theme — so no paper mat around it
    // (DESIGN.md §2.8: the paper doctrine is for canvases we cannot theme).
    surface: "app",
    // Cheap to rebuild: the parsed document lives in the notebook store, so a
    // tab switch re-renders from memory rather than re-reading the file.
    keepMounted: false,
  },
  commands: [
    {
      id: "notebook.open",
      title: "Open notebook…",
      detail: () => "read a .ipynb in its own pane",
      run: openNotebookFlow,
    },
  ],
  // No chord. `Alt` is the only modifier `shortcuts.md` may bind, so every
  // chord a tool takes is one the user can no longer choose — and this command
  // is reached by name, not by reflex (docs/tools.md).
  onWorkspaceChanged: () => {
    // Every path in the store belonged to the workspace that is gone.
    useNotebookStore.getState().reset();
  },
};
