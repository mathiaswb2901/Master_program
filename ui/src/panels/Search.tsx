/**
 * Search — workspace-wide content search (M7 V7b, Ctrl+Shift+F).
 *
 * A whole capability in one module (plus `search.ts` and its stylesheet): the
 * query box, the results grouped by file, click-to-open a hit in the editor at
 * its line, and the command that opens the panel. It edits no shared file but the
 * one line in `tools.ts`.
 *
 * **Singular, deliberately.** Unlike the editor and Review panes — each *bound to*
 * a file or a result it is a view onto — a search pane points at no resource: its
 * whole state is the query you typed, which nothing else references and nothing
 * restores. A second one would be a second empty box, not a second view of
 * anything, so this is one panel (the VS Code "Search" view), opened on demand and
 * brought forward when asked again. Its state lives in local component state for
 * the same reason — there is no id to key a shared store by (CLAUDE.md: a
 * singleton assumption owes a comment saying why the window really has only one;
 * this is it).
 *
 * The results carry the server's honest signals straight through: an empty result
 * says "no matches" rather than showing blankness (AXI shape 2), and a truncated
 * one names the cap and how to widen it (AXI shape 1).
 */

import type { IDockviewPanelProps } from "dockview";
import { useEffect, useRef, useState } from "react";

import { searchWorkspace } from "../api";
import type { WorkbenchTool } from "../registry";
import { openHit } from "../search";
import type { FileMatches, SearchHit, SearchResponse } from "../types";

import { openPanel } from "../dock";

import "../styles/search.css";

const TOOL_ID = "search";

/** How much of a line to bring back is the server's call; the UI just lays it
 * out. This is the default hit cap the panel asks for — generous, since a person
 * scans a list the agent would not. */
const PANEL_MAX_RESULTS = 500;

type Phase = "idle" | "loading" | "done" | "error";

// ---- one hit row ------------------------------------------------------------

/** A matching line: its 1-based number and the line, with the matched span
 * marked. Clicking it opens the file at the line (`search.ts`). */
export function HitRow({ path, hit, query }: { path: string; hit: SearchHit; query: string }) {
  return (
    <button
      type="button"
      className="wb-search-hit"
      data-line={hit.line}
      onClick={() => void openHit(path, hit.line)}
    >
      <span className="wb-search-line u-tabular">{hit.line}</span>
      <span className="wb-search-text">{highlight(hit.text, hit.col, query.length)}</span>
    </button>
  );
}

/** The line with the matched span wrapped in `<mark>`. The match sits at `col`
 * and is `length` long (a substring match); a clip that cut the match short is
 * clamped so the mark never runs past the visible text. */
function highlight(text: string, col: number, length: number) {
  if (col < 0 || col >= text.length || length <= 0) return text;
  const end = Math.min(col + length, text.length);
  return (
    <>
      {text.slice(0, col)}
      <mark className="wb-search-mark">{text.slice(col, end)}</mark>
      {text.slice(end)}
    </>
  );
}

// ---- one file's group -------------------------------------------------------

/** A file header and its hits. The header is a button too — it opens the file at
 * its first hit, so the whole group is reachable without picking a line. */
export function FileGroup({ file, query }: { file: FileMatches; query: string }) {
  const first = file.hits[0];
  return (
    <li className="wb-search-file" data-path={file.path}>
      <button
        type="button"
        className="wb-search-file-head"
        title={file.path}
        onClick={() => first !== undefined && void openHit(file.path, first.line)}
      >
        <span className="wb-search-file-path u-truncate">{file.path}</span>
        <span className="wb-search-file-count u-tabular">{file.hits.length}</span>
      </button>
      <ul className="wb-search-hits">
        {file.hits.map((hit, index) => (
          <HitRow key={`${String(hit.line)}:${String(index)}`} path={file.path} hit={hit} query={query} />
        ))}
      </ul>
    </li>
  );
}

// ---- the results view (pure over props) -------------------------------------

/** Everything a settled search says: the grouped files, or the honest "none",
 * plus the truncation note when the cap was hit. Pure so every state renders
 * under a static test. */
export function SearchResults({ results }: { results: SearchResponse }) {
  if (results.files.length === 0) {
    return (
      <div className="wb-search-none">
        <span>
          No matches for “{results.query}”. Try a shorter or different query.
        </span>
      </div>
    );
  }
  return (
    <>
      <p className="wb-search-summary u-tabular">
        {results.total_hits} match{results.total_hits === 1 ? "" : "es"} in{" "}
        {results.files_with_matches} file{results.files_with_matches === 1 ? "" : "s"}
      </p>
      <ul className="wb-search-results">
        {results.files.map((file) => (
          <FileGroup key={file.path} file={file} query={results.query} />
        ))}
      </ul>
      {results.truncated && (
        <p className="wb-search-truncation">
          Showing the first {results.total_hits}. Narrow the query to see the rest.
        </p>
      )}
    </>
  );
}

// ---- the panel --------------------------------------------------------------

export function SearchPanel(_props: IDockviewPanelProps) {
  const [query, setQuery] = useState("");
  const [phase, setPhase] = useState<Phase>("idle");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const runSearch = (): void => {
    const q = query.trim();
    if (q === "") {
      setResults(null);
      setPhase("idle");
      return;
    }
    setPhase("loading");
    setError(null);
    void searchWorkspace({ query: q, max_results: PANEL_MAX_RESULTS })
      .then((response) => {
        setResults(response);
        setPhase("done");
      })
      .catch(() => {
        setError("Search failed. Try again.");
        setPhase("error");
      });
  };

  return (
    <div className="wb-search">
      <form
        className="wb-search-form"
        onSubmit={(e) => {
          e.preventDefault();
          runSearch();
        }}
      >
        <input
          ref={inputRef}
          type="search"
          className="wb-search-input"
          placeholder="Find in files…"
          aria-label="Search in files"
          spellCheck={false}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </form>
      <div className="wb-search-body">
        {phase === "idle" && (
          <div className="wb-search-empty">
            <span>Search the workspace for text. Results group by file — click one to open it.</span>
          </div>
        )}
        {phase === "loading" && <p className="wb-search-loading">Searching…</p>}
        {phase === "error" && error !== null && (
          <p className="wb-search-error" role="alert">
            {error}
          </p>
        )}
        {phase === "done" && results !== null && <SearchResults results={results} />}
      </div>
    </div>
  );
}

// ---- registration -----------------------------------------------------------

function SearchIcon() {
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
      <circle cx="7" cy="7" r="4.5" />
      <path d="M10.5 10.5 14 14" />
    </svg>
  );
}

export const searchTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Search",
  icon: SearchIcon,
  panel: {
    component: SearchPanel,
    defaultLocation: { area: "left", size: 320 },
    // Opened on demand — a search is something you go to when you need it, not a
    // pane that takes room from your work. Singular: see the module docstring.
    openByDefault: false,
    singleton: true,
  },
  commands: [
    {
      id: "search.open",
      title: "Search in files…",
      detail: () => "find text across the workspace and jump to a hit",
      run: () => openPanel(TOOL_ID),
    },
  ],
  // Ctrl+Shift+F is the universal find-in-files chord — the command earns a
  // registered chord (a reflex the muscle memory expects), which `shortcuts.md`
  // may then not rebind out from under it.
  shortcuts: { "search.open": ["Ctrl+Shift+F"] },
};
