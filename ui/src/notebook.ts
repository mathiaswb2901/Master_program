/**
 * The notebook capability's own half: what counts as a notebook, the store that
 * holds the parsed ones, and the token pass that colours a code cell.
 *
 * Deliberately free of React and of `store.ts` — everything here is data or a
 * pure function, so the rules that decide what a user sees are unit-tested
 * rather than assumed (`notebook.test.ts`).
 *
 * ## The store is this tool's own
 *
 * A second `create()` instance in the module that owns it, on the one condition
 * CLAUDE.md puts on that: nothing outside this capability reads it. It also
 * subscribes to the shared `/ws/events` socket for its own frames rather than
 * adding a branch to the app store's dispatch — the pattern `usage.ts` and
 * `activity.ts` established.
 *
 * ## Read-only, and that is the feature
 *
 * There is no execution here, no kernel and no write path, because there is
 * none on the server either (ROADMAP M4). A run button that cannot run is worse
 * than no button: the honest UI is the one that never offers it.
 */

import { create } from "zustand";

import * as api from "./api";
import type { NotebookDocument, WorkspaceEvent } from "./types";
import { ReconnectingSocket } from "./ws";

/** The one extension. Compared lowercased — a naming convention, not a claim
 * about whether the user's filesystem folds case. */
export const NOTEBOOK_EXTENSION = "ipynb";

export function isNotebookPath(path: string): boolean {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 && name.slice(dot + 1).toLowerCase() === NOTEBOOK_EXTENSION;
}

/** Pane and tab label: the file name, which is what the user calls it. */
export function notebookTitle(path: string): string {
  return path.split("/").pop() ?? path;
}

// ---- the store ---------------------------------------------------------------

/**
 * Four states, and each one has its own rendering — which is the point of
 * naming them rather than deriving "is there a document?" at the call site:
 *
 * - `loading` — asked, nothing back yet;
 * - `ready` — parsed. May still carry a schema complaint (`doc.valid`);
 * - `gone` — the file is not there (404). A **tombstone**, because a restored
 *   pane names a file the workspace may no longer have;
 * - `failed` — it is there and will not parse (or the read was refused). A
 *   **degraded card**, because the recovery is different: there is a file to
 *   look at, and the card says what failed and offers it.
 */
export type NotebookStatus = "loading" | "ready" | "gone" | "failed";

export interface NotebookEntry {
  status: NotebookStatus;
  doc: NotebookDocument | null;
  /** The server's reason, verbatim. Rendered — never a category we invented. */
  error: string | null;
}

interface NotebookStore {
  /** By workspace-relative path. Shared by every pane pointed at that path, so
   * the same notebook open twice is fetched once. */
  notebooks: Record<string, NotebookEntry>;
  /** Read (or re-read) one notebook. Concurrent calls for the same path share
   * one request; `reload` bypasses the "already loaded" short-circuit. */
  load: (path: string, reload?: boolean) => Promise<void>;
  /** Drop everything — the workspace moved and none of these paths mean what
   * they meant (`onWorkspaceChanged`). */
  reset: () => void;
}

/** In-flight reads, so two panes on one notebook are one request. */
const inFlight = new Set<string>();

export const useNotebookStore = create<NotebookStore>((set, get) => ({
  notebooks: {},

  load: async (path, reload = false) => {
    const existing = get().notebooks[path];
    if (!reload && existing !== undefined && existing.status !== "gone") return;
    if (inFlight.has(path)) return;
    inFlight.add(path);
    // Keep whatever is on screen while a reload is in flight: a watcher event
    // must not blank a notebook the user is reading mid-scroll.
    set((s) => ({
      notebooks: {
        ...s.notebooks,
        [path]: { status: "loading", doc: existing?.doc ?? null, error: null },
      },
    }));
    try {
      const doc = await api.getNotebook(path);
      set((s) => ({ notebooks: { ...s.notebooks, [path]: { status: "ready", doc, error: null } } }));
    } catch (err) {
      const status: NotebookStatus =
        err instanceof api.ApiError && err.status === 404 ? "gone" : "failed";
      const error = err instanceof api.ApiError ? err.detail : String(err);
      set((s) => ({ notebooks: { ...s.notebooks, [path]: { status, doc: null, error } } }));
    } finally {
      inFlight.delete(path);
    }
  },

  reset: () => {
    inFlight.clear();
    set({ notebooks: {} });
  },
}));

/**
 * Follow the disk, because the disk is the source of truth (CLAUDE.md).
 *
 * A notebook is the output of something that ran elsewhere — a script, an
 * agent, a `jupyter nbconvert` — so "it changed while you were looking at it"
 * is the *normal* case here rather than the exceptional one. Only paths this
 * store already holds are re-read: an event for a notebook nobody has open
 * costs nothing.
 *
 * Its own socket, filtered for its own frames, rather than a branch in the app
 * store's dispatch (`usage.ts`, `activity.ts`).
 */
let subscribed = false;

export function watchNotebooks(): void {
  if (subscribed) return;
  subscribed = true;
  new ReconnectingSocket("/ws/events", {
    onMessage: (data) => {
      const event = data as WorkspaceEvent;
      if (event.type !== "file_changed" || event.is_dir) return;
      if (!isNotebookPath(event.path)) return;
      if (useNotebookStore.getState().notebooks[event.path] === undefined) return;
      if (event.change === "deleted") {
        useNotebookStore.setState((s) => ({
          notebooks: {
            ...s.notebooks,
            [event.path]: { status: "gone", doc: null, error: null },
          },
        }));
        return;
      }
      void useNotebookStore.getState().load(event.path, true);
    },
    // A reconnect means events were missed; re-read what is open rather than
    // trusting a snapshot taken before the gap.
    onOpen: () => {
      for (const path of Object.keys(useNotebookStore.getState().notebooks)) {
        void useNotebookStore.getState().load(path, true);
      }
    },
  });
}

// ---- syntax highlighting -----------------------------------------------------
//
// A token pass rather than an editor, and rather than Monaco's `colorize`.
//
// Monaco is 3.3 MB behind a dynamic import (`monaco.ts`) and mounting one
// editor per cell would put fifty of them in a scroll container — the wrong
// tool for text nobody can type into. `editor.colorize` avoids that but returns
// an HTML *string*, which would mean the app's first `dangerouslySetInnerHTML`,
// for output that is not even user-authored markup. So this is the same call
// `markdown.tsx` made for chat: a small hand-rolled pass whose output is React
// text nodes, with no injection surface anywhere on the path.
//
// It colours **Python**, and says so. Strings and numbers are lexed the same way
// in every language a notebook is likely to hold, so those are coloured for any
// language; keywords and `#` comments are Python's and are applied only when the
// notebook says it is Python. A half-right highlighter for R is worse than an
// honest plain rendering of it.

export type CodeTokenKind = "plain" | "comment" | "string" | "number" | "keyword" | "builtin";

export interface CodeToken {
  kind: CodeTokenKind;
  text: string;
}

/** Python 3.12's full keyword list plus the soft keywords. */
const PYTHON_KEYWORDS: ReadonlySet<string> = new Set([
  "False", "None", "True", "and", "as", "assert", "async", "await", "break", "case", "class",
  "continue", "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if",
  "import", "in", "is", "lambda", "match", "nonlocal", "not", "or", "pass", "raise", "return",
  "try", "type", "while", "with", "yield",
]);

/** The builtins an analyst's cell actually contains. Not the whole of
 * `builtins` — a list nobody maintains is one that goes wrong quietly. */
const PYTHON_BUILTINS: ReadonlySet<string> = new Set([
  "abs", "all", "any", "bool", "dict", "enumerate", "float", "getattr", "hasattr", "int", "isinstance",
  "len", "list", "map", "max", "min", "open", "print", "range", "repr", "round", "set", "setattr",
  "sorted", "str", "sum", "super", "tuple", "type", "zip",
]);

/** Language ids that mean Python. `language_info.name` is `python`; a kernelspec
 * language may say `python3`, and IPython magics land under `ipython`. */
const PYTHON_LANGUAGES: ReadonlySet<string> = new Set(["python", "python2", "python3", "ipython"]);

/**
 * Earliest-match-wins, one pass. Order in the alternation resolves overlaps at
 * the same index — triple-quoted strings before single-quoted ones, or a
 * docstring's first two quotes would close an empty string and the rest of the
 * file would highlight inside out.
 */
const PY_TOKENS = new RegExp(
  '("""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\')' + // 1 triple-quoted
    '|("(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\')' + // 2 quoted
    "|(#[^\\n]*)" + // 3 comment
    "|(\\b0[xXbBoO][0-9a-fA-F_]+\\b|\\b\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?\\b)" + // 4 number
    "|([A-Za-z_][A-Za-z0-9_]*)", // 5 word
  "g",
);

/** Same, minus the `#` comment and the word rule: strings and numbers only. */
const GENERIC_TOKENS = new RegExp(
  '("""[\\s\\S]*?"""|\'\'\'[\\s\\S]*?\'\'\')' +
    '|("(?:\\\\.|[^"\\\\\\n])*"|\'(?:\\\\.|[^\'\\\\\\n])*\')' +
    "|(\\b0[xX][0-9a-fA-F_]+\\b|\\b\\d[\\d_]*(?:\\.\\d[\\d_]*)?(?:[eE][+-]?\\d+)?\\b)",
  "g",
);

/**
 * Split source into coloured runs. Concatenating every `text` reproduces the
 * input exactly — asserted in `notebook.test.ts`, because a highlighter that
 * drops a character is a viewer that lies about the file.
 */
export function tokenizeCode(source: string, language: string): CodeToken[] {
  const python = PYTHON_LANGUAGES.has(language.toLowerCase());
  const pattern = python ? PY_TOKENS : GENERIC_TOKENS;
  const out: CodeToken[] = [];
  let cursor = 0;
  const push = (kind: CodeTokenKind, text: string): void => {
    if (text === "") return;
    const last = out[out.length - 1];
    // Merge adjacent runs of the same kind so a line of plain code is one span
    // rather than one per character class.
    if (last !== undefined && last.kind === kind) last.text += text;
    else out.push({ kind, text });
  };

  for (const match of source.matchAll(pattern)) {
    const index = match.index ?? 0;
    push("plain", source.slice(cursor, index));
    const token = match[0];
    if (token.startsWith("#")) push("comment", token);
    else if (token.startsWith('"') || token.startsWith("'")) push("string", token);
    else if (/^[0-9]/.test(token)) push("number", token);
    else if (PYTHON_KEYWORDS.has(token)) push("keyword", token);
    else if (PYTHON_BUILTINS.has(token)) push("builtin", token);
    else push("plain", token);
    cursor = index + token.length;
  }
  push("plain", source.slice(cursor));
  return out;
}

// ---- what a cell says about itself -------------------------------------------

/** The `[n]` beside a code cell — `[ ]` for one that was never run, which is a
 * real and different state from "ran and produced nothing". */
export function executionLabel(count: number | null): string {
  return count === null ? "[ ]" : `[${String(count)}]`;
}

/** "3,412 characters" etc. — the phrasing every truncation notice shares, so a
 * cap is always reported in the same words with the same separators. */
export function countPhrase(n: number, noun: string): string {
  return `${n.toLocaleString("en-US")} ${noun}${n === 1 ? "" : "s"}`;
}

/** Powers of 1024, named the way the user's own file manager names them — this
 * number appears in one sentence ("the figure you are not being shown is 35.2
 * MB"), and a reader comparing it to what Explorer says about the notebook
 * should not have to know which of the two conventions each side picked. One
 * decimal above a kilobyte; none below, where a fraction of a byte is noise. */
export function byteSize(bytes: number): string {
  const units = ["bytes", "KB", "MB", "GB"];
  let value = Math.max(bytes, 0);
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const digits = unit === 0 ? 0 : 1;
  return `${value.toFixed(digits)} ${units[unit]}`;
}
