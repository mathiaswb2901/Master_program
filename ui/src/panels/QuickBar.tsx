import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { visibleCommands } from "../commands";
import { chordKeycaps } from "../keys";
import { useStore } from "../store";
import type { TreeNode } from "../types";

/** Subsequence match with bonuses for adjacency and segment starts; null = no match. */
function fuzzyScore(query: string, target: string): number | null {
  if (query === "") return 0;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  let score = 0;
  let last = -2;
  for (let i = 0; i < t.length && qi < q.length; i++) {
    if (t[i] !== q[qi]) continue;
    score += 1;
    if (i === last + 1) score += 2;
    const prev = i > 0 ? t[i - 1] : "";
    if (i === 0 || prev === "/" || prev === "." || prev === "_" || prev === "-" || prev === " ") {
      score += 3;
    }
    last = i;
    qi += 1;
  }
  if (qi < q.length) return null;
  return score - t.length * 0.01; // light tiebreak toward shorter targets
}

interface Row {
  key: string;
  title: string;
  detail: string;
  /** Primary chord, rendered as keycaps on the right (DESIGN.md §6.5). */
  chord?: string;
  /** Section this row belongs to; a header is drawn where it changes. */
  category?: string;
  run: () => void;
}

export function QuickBar() {
  const open = useStore((s) => s.quickBarOpen);
  const prefill = useStore((s) => s.quickBarPrefill);
  const tree = useStore((s) => s.tree);
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const selRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) {
      setQuery(prefill);
      setSel(0);
    }
  }, [open, prefill]);

  useEffect(() => {
    selRef.current?.scrollIntoView({ block: "nearest" });
  }, [sel, query]);

  const files = useMemo(() => {
    const out: string[] = [];
    const walk = (node: TreeNode): void => {
      if (node.kind === "file") out.push(node.path);
      else (node.children ?? []).forEach(walk);
    };
    if (tree) walk(tree);
    return out;
  }, [tree]);

  if (!open) return null;
  const close = (): void => useStore.getState().setQuickBarOpen(false);

  const actionsMode = query.startsWith(">");
  let rows: Row[];
  if (actionsMode) {
    const q = query.slice(1).trim();
    // Every command in the registry, so the QuickBar is the complete keyboard
    // path to the app — nothing is reachable only by mouse or only by chord.
    rows = visibleCommands()
      .map((command) => ({ command, score: fuzzyScore(q, command.title) }))
      .filter((x) => x.score !== null)
      // Categorized commands (shortcuts.md) sort after the built-ins, so their
      // section header stays a header rather than appearing mid-list.
      .sort(
        (a, b) =>
          (a.command.category === undefined ? 0 : 1) -
            (b.command.category === undefined ? 0 : 1) || (b.score ?? 0) - (a.score ?? 0),
      )
      .map(({ command }) => ({
        key: command.id,
        title: command.title,
        detail: command.detail?.() ?? "",
        chord: command.keys?.[0],
        category: command.category,
        run: command.run,
      }));
  } else {
    const q = query.trim();
    rows = files
      .map((path) => ({ path, score: fuzzyScore(q, path) }))
      .filter((x) => x.score !== null)
      .sort((a, b) => (b.score ?? 0) - (a.score ?? 0))
      .slice(0, 50)
      .map(({ path }) => ({
        key: path,
        title: path.split("/").pop() ?? path,
        detail: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "",
        run: () => void useStore.getState().openFile(path),
      }));
  }
  const selIdx = Math.min(sel, Math.max(0, rows.length - 1));

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel(Math.min(selIdx + 1, rows.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel(Math.max(selIdx - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const row = rows[selIdx];
      if (row) {
        row.run();
        close();
      }
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    }
  };

  return (
    <>
      <div className="wb-qb-backdrop" onClick={close} />
      <div className="wb-qb" role="dialog" aria-label="Quick open">
        <input
          autoFocus
          className="wb-qb-input"
          placeholder="Search files — type > for actions"
          value={query}
          spellCheck={false}
          onChange={(e) => {
            setQuery(e.target.value);
            setSel(0);
          }}
          onKeyDown={onKeyDown}
        />
        <div className="wb-qb-results">
          {rows.length === 0 && (
            <div className="wb-qb-empty">
              {actionsMode ? "No matching actions" : "No matching files"}
            </div>
          )}
          {rows.map((row, i) => (
            <Fragment key={row.key}>
              {row.category !== undefined && row.category !== rows[i - 1]?.category && (
                <div className="wb-qb-cat">{row.category}</div>
              )}
              <button
                type="button"
                ref={i === selIdx ? selRef : undefined}
                className={"wb-qb-row" + (i === selIdx ? " is-selected" : "")}
                onClick={() => {
                  row.run();
                  close();
                }}
                onMouseMove={() => setSel(i)}
              >
                <span className="wb-qb-row-title u-truncate">{row.title}</span>
                <span className="wb-qb-row-detail u-truncate">{row.detail}</span>
                {row.chord !== undefined && (
                  <span className="wb-qb-row-keys">
                    {chordKeycaps(row.chord).map((cap) => (
                      <span key={cap} className="wb-keycap">
                        {cap}
                      </span>
                    ))}
                  </span>
                )}
              </button>
            </Fragment>
          ))}
        </div>
        <div className="wb-qb-footer">
          <span>
            <span className="wb-keycap">↑↓</span> navigate
          </span>
          <span>
            <span className="wb-keycap">Enter</span> open
          </span>
          <span>
            <span className="wb-keycap">&gt;</span> actions
          </span>
          <span>
            <span className="wb-keycap">Esc</span> close
          </span>
        </div>
      </div>
    </>
  );
}
