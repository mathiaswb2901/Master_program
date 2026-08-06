import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { useVisibleCommands } from "../commands";
import { chordKeycaps } from "../keys";
import { usePresence } from "../motion";
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
  /** Visible but not choosable — the row is here so the reason in `detail` is
   * (DESIGN.md §6.5). Only a `quickPick` supplies these. */
  disabled?: boolean;
  run: () => void;
}

/** Best of the two things a row shows. A pick row's detail is often the part
 * the user is aiming at (a file's folder, a session's folder), so a query that
 * matches only the detail must still find the row. */
function rowScore(query: string, title: string, detail: string): number | null {
  const a = fuzzyScore(query, title);
  const b = detail === "" ? null : fuzzyScore(query, detail);
  if (a === null) return b;
  if (b === null) return a;
  return Math.max(a, b);
}

export function QuickBar() {
  const open = useStore((s) => s.quickBarOpen);
  const prefill = useStore((s) => s.quickBarPrefill);
  const pick = useStore((s) => s.quickPick);
  const tree = useStore((s) => s.tree);
  // Subscribed, not sampled: the command set grows after launch (see the hook).
  const commands = useVisibleCommands();
  const [query, setQuery] = useState("");
  const [sel, setSel] = useState(0);
  const selRef = useRef<HTMLButtonElement>(null);
  // Held on screen for `--motion-exit-ms` after it closes, so dismissing it is
  // a movement rather than a disappearance (DESIGN.md §5).
  const [present, leaving] = usePresence(open);

  useEffect(() => {
    if (open) {
      setQuery(prefill);
      setSel(0);
      // The file list is the whole workspace in one walk, and this is the only
      // surface that wants it. Fetched here — when the user asks to search —
      // rather than at launch or on every watcher event.
      void useStore.getState().ensureFileIndex();
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

  if (!present) return null;
  const close = (): void => useStore.getState().setQuickBarOpen(false);
  const exiting = leaving ? " is-leaving" : "";

  // Three modes, one surface. A `quickPick` is a list some capability supplied
  // (`store.ts`); it wins, because it was opened *for* that list — the `>`
  // prefix is a file/command toggle and has no meaning inside one.
  const actionsMode = pick === null && query.startsWith(">");
  let rows: Row[];
  if (pick !== null) {
    const q = query.trim();
    const supplied = pick.rows(q);
    // Ranked inside each section, never across them: the sections are the
    // vocabulary ("Panels", "Agent sessions", "Files"), and a list that
    // reshuffles its headers on every keystroke is unreadable.
    const sections = [...new Set(supplied.map((row) => row.category ?? ""))];
    rows = supplied
      .map((row) => ({ row, score: rowScore(q, row.title, row.detail ?? "") }))
      .filter((scored) => scored.score !== null)
      .sort(
        (a, b) =>
          sections.indexOf(a.row.category ?? "") - sections.indexOf(b.row.category ?? "") ||
          (b.score ?? 0) - (a.score ?? 0),
      )
      .map(({ row }) => ({
        key: row.key,
        title: row.title,
        detail: row.detail ?? "",
        ...(row.category !== undefined ? { category: row.category } : {}),
        ...(row.disabled === true ? { disabled: true } : {}),
        run: row.run,
      }));
  } else if (actionsMode) {
    const q = query.slice(1).trim();
    // Every command in the registry, so the QuickBar is the complete keyboard
    // path to the app — nothing is reachable only by mouse or only by chord.
    rows = commands
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
  // The selection only ever lands on a row that can be run: a disabled row is
  // there to be *read* (why "New agent session" is unavailable), and arrowing
  // onto it would leave Enter doing nothing with no explanation.
  const nextRunnable = (from: number, step: number): number => {
    for (let i = from + step; i >= 0 && i < rows.length; i += step) {
      if (rows[i]?.disabled !== true) return i;
    }
    return from;
  };
  /** Nearest runnable row: forwards first, then backwards. Answers `from` when
   * every row is disabled, which the Enter guard below then declines. */
  const runnableFrom = (from: number): number => {
    const ahead = nextRunnable(from, 1);
    return ahead === from ? nextRunnable(from, -1) : ahead;
  };
  const clamped = Math.min(sel, Math.max(0, rows.length - 1));
  const selIdx = rows[clamped]?.disabled === true ? runnableFrom(clamped) : clamped;

  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>): void => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel(nextRunnable(selIdx, 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel(nextRunnable(selIdx, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const row = rows[selIdx];
      if (row && row.disabled !== true) {
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
      <div className={"wb-qb-backdrop" + exiting} onClick={close} />
      {/* No `aria-hidden` while it leaves: the input inside still holds focus
          for those few frames, and hiding a focused subtree from the
          accessibility tree is worse than describing a dialog that is fading.
          `pointer-events: none` in the stylesheet is what makes it inert. */}
      <div className={"wb-qb" + exiting} role="dialog" aria-label={pick?.label ?? "Quick open"}>
        <input
          autoFocus
          className="wb-qb-input"
          placeholder={pick?.placeholder ?? "Search files — type > for actions"}
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
              {pick !== null
                ? "Nothing matches"
                : actionsMode
                  ? "No matching actions"
                  : "No matching files"}
            </div>
          )}
          {rows.map((row, i) => (
            <Fragment key={row.key}>
              {row.category !== undefined && row.category !== rows[i - 1]?.category && (
                <div className="wb-qb-cat">{row.category}</div>
              )}
              <button
                type="button"
                disabled={row.disabled === true}
                ref={i === selIdx ? selRef : undefined}
                className={"wb-qb-row" + (i === selIdx ? " is-selected" : "")}
                onClick={() => {
                  row.run();
                  close();
                }}
                onMouseMove={() => {
                  if (row.disabled !== true) setSel(i);
                }}
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
          {pick === null && (
            <span>
              <span className="wb-keycap">&gt;</span> actions
            </span>
          )}
          <span>
            <span className="wb-keycap">Esc</span> {pick === null ? "close" : "cancel"}
          </span>
        </div>
      </div>
    </>
  );
}
