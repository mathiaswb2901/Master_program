import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";

import { useVisibleCommands } from "../commands";
import { chordKeycaps } from "../keys";
import { usePresence } from "../motion";
import { useOverlayKeys } from "../overlays";
import { useStore } from "../store";
import type { TreeNode } from "../types";

/**
 * The palette's popup, named once.
 *
 * A fixed id rather than a generated one because there is exactly one QuickBar
 * in a window — `App.tsx` mounts it beside the dock, not inside a pane, so the
 * "panes are instances" rule does not reach it (CLAUDE.md). If it ever becomes
 * per-pane this constant is where that breaks, loudly, as duplicate ids.
 */
const LISTBOX_ID = "wb-qb-listbox";

/** The id `aria-activedescendant` points at. Index-based, like the selection. */
const optionId = (index: number): string => `wb-qb-option-${String(index)}`;

/**
 * Everything inside the dialog the Tab ring may land on.
 *
 * `:not([tabindex="-1"])` is applied to *every* clause rather than only to
 * `[tabindex]`: the result rows are real `<button>`s (they must be, for click,
 * for `:disabled` and for the chrome the stylesheet hangs off them) and would
 * otherwise be matched by the `button` clause and dragged back into the ring —
 * which is the tab order this fix exists to get rid of.
 */
const FOCUSABLE = ["a[href]", "button", "input", "select", "textarea", "[tabindex]"]
  .map((tag) => `${tag}:not([disabled]):not([tabindex="-1"])`)
  .join(",");

/**
 * How long a keystroke has to be the last one before the count is spoken.
 *
 * A live region that fires on every character turns a five-letter query into
 * five interruptions, and a screen reader queues them: the user hears the
 * result of "t", "te", "ter"… long after they have moved on. One announcement
 * per pause is the whole point of the region.
 */
const ANNOUNCE_DEBOUNCE_MS = 250;

/**
 * The result count, spoken politely once the typing stops.
 *
 * Its own component for a plain mechanical reason: the row list is computed
 * *after* `QuickBar`'s early return, so a hook that depends on how many rows
 * there are cannot live in that component. It is also the right seam — this is
 * the only part of the overlay that exists for a reader who cannot see the
 * list, and it renders nothing a sighted user can perceive (`u-sr-only`,
 * `position: absolute`, so it takes no part in the flex column above it).
 */
function ResultCount({ count }: { count: number }) {
  const [text, setText] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setText(count === 0 ? "No results" : `${String(count)} result${count === 1 ? "" : "s"}`);
    }, ANNOUNCE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [count]);
  // Mounted empty and filled a beat later, on purpose: assistive tech announces
  // a live region's *changes*, not the text it found there when it arrived.
  return (
    <div className="u-sr-only" role="status" aria-live="polite">
      {text}
    </div>
  );
}

/**
 * Subsequence match with bonuses for adjacency and segment starts.
 *
 * Returns the score *and* the character positions it consumed, because the row
 * has to be able to say why it is here (DESIGN.md §6.5's match highlight) and a
 * second walk that disagreed with this one would highlight the wrong letters.
 * `null` = no match.
 */
function fuzzyMatch(query: string, target: string): { score: number; hits: number[] } | null {
  if (query === "") return { score: 0, hits: [] };
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  const hits: number[] = [];
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
    hits.push(i);
    last = i;
    qi += 1;
  }
  if (qi < q.length) return null;
  return { score: score - t.length * 0.01, hits }; // light tiebreak toward shorter targets
}

/** The score alone, for the ranking that does not care where the match landed. */
function fuzzyScore(query: string, target: string): number | null {
  return fuzzyMatch(query, target)?.score ?? null;
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
  /** Character positions to emphasise, where the row already knows them —
   * a file is *ranked* on its whole path and *shown* as a name beside a folder,
   * so the match that found it is split across the two labels rather than
   * guessed at again from either half (see `Labelled`). */
  titleHits?: number[];
  detailHits?: number[];
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

/**
 * A label with the matched characters held at full strength (DESIGN.md §6.5).
 *
 * The emphasis has to land under the letters the user can actually see, and a
 * row's two labels are not always what the ranking matched. Where the row knows
 * (a file, ranked on its whole path and shown as a name beside its folder) it
 * hands the positions down already split between the two; where it does not (a
 * command, a pick row — both matched on the text they show) the query is matched
 * against this label. Nothing is highlighted that did not match, in either case.
 */
function Labelled({
  text,
  query,
  className,
  positions,
}: {
  text: string;
  query: string;
  className: string;
  positions?: number[] | undefined;
}) {
  const hits = positions ?? (query === "" ? [] : (fuzzyMatch(query, text)?.hits ?? []));
  if (hits.length === 0) return <span className={`${className} u-truncate`}>{text}</span>;
  const marked = new Set(hits);
  // Adjacent characters are coalesced into one span: a word matched end to end
  // should be one bold run, not six elements the browser lays out separately.
  const parts: { text: string; hit: boolean }[] = [];
  for (let i = 0; i < text.length; i++) {
    const hit = marked.has(i);
    const last = parts[parts.length - 1];
    if (last !== undefined && last.hit === hit) last.text += text[i];
    else parts.push({ text: text[i] ?? "", hit });
  }
  return (
    <span className={`${className} u-truncate is-matched`}>
      {parts.map((part, i) =>
        part.hit ? (
          <span key={i} className="wb-qb-hit">
            {part.text}
          </span>
        ) : (
          <Fragment key={i}>{part.text}</Fragment>
        ),
      )}
    </span>
  );
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
  const inputRef = useRef<HTMLInputElement>(null);
  const selRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Where the keyboard was when the palette took it. Restored on a *cancel*
  // only — see `dismiss` and `choose` below.
  const invokerRef = useRef<HTMLElement | null>(null);
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

  /**
   * The keyboard, on every open — which is not the same as on every mount.
   *
   * `autoFocus` is a mount-time attribute, and this overlay does not remount
   * each time it opens: it is held on screen for `--motion-exit-ms` after it
   * closes (`usePresence`, above), so an open inside that window reuses
   * the input that is already there and nothing focuses it a second time. Focus
   * is then wherever the dismissal left it — the row a mouse click just ran,
   * most often — and the palette answers no keys at all: Escape does not close
   * it, typing does not filter it, and the only way out is the backdrop. The
   * window is `--motion-exit-ms` *at least*: it is a `setTimeout`, so a busy
   * main thread (a launch, a layout switch) stretches it.
   *
   * Two dependencies beyond `open`, for the two ways in. `present` because the
   * first render of an open QuickBar has no input to focus — `usePresence`
   * flips it from an effect of its own, so the element exists one commit later.
   * `prefill` because a chord pressed *at* an open palette (`Ctrl+Shift+P` over
   * a file search) reopens it in the other mode, which the effect above already
   * treats as a fresh open; the keyboard has to agree with the query it reset.
   */
  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open, prefill, present]);

  /**
   * Who to give the keyboard back to, remembered at the moment it is taken.
   *
   * Read on the same three triggers as the focus effect above, and for the same
   * reason — a chord pressed *at* an open palette is a fresh open. An open that
   * happens inside the exit window finds focus already inside the overlay (the
   * row a click just ran); that is not an invoker, so the one already recorded
   * stands and the palette does not learn to hand focus back to a button it is
   * about to unmount.
   */
  useEffect(() => {
    if (!open) return;
    const active = document.activeElement;
    if (active instanceof HTMLElement && dialogRef.current?.contains(active) !== true) {
      invokerRef.current = active;
    }
  }, [open, prefill, present]);

  /** Cancel: close, and put the keyboard back where it was. */
  const dismiss = useCallback((): void => {
    const invoker = invokerRef.current;
    invokerRef.current = null;
    // Synchronously, before the store update: the focus effect above only fires
    // while `open`, so nothing races this back onto the input. `document.body`
    // is not a focus target — it is what "nothing was focused" looks like.
    if (invoker !== null && invoker !== document.body && invoker.isConnected) invoker.focus();
    useStore.getState().setQuickBarOpen(false);
  }, []);

  /**
   * The modal keyboard, at the window and in the capture phase.
   *
   * Two keys, one listener, and both halves of the audit's first finding.
   *
   * **Escape** was an `onKeyDown` on the input, which meant it worked only
   * while the input had focus — and one `Shift+Tab` was enough to lose that,
   * because there was no trap. So the palette could be left open with its
   * keyboard on a control behind the scrim, invisible under 60% black and
   * unreachable by mouse, with nothing but the backdrop as a way out. Capture
   * at the window is the pattern `Modal.tsx` already ships, and it buys the
   * same thing here that it buys there: Esc wins over Monaco and xterm.
   *
   * **Tab** cycles inside the dialog and never leaves it. In practice the ring
   * has one stop — the combobox — because the options are addressed with
   * `aria-activedescendant` and are deliberately not tab stops (the ARIA
   * combobox pattern; fifty file rows in the tab order was its own bug). It is
   * written as a ring rather than as "swallow Tab" so a control added to this
   * overlay later is reachable without anyone remembering this function.
   *
   * Registered through `useOverlayKeys` rather than directly at the window,
   * which is what keeps it from answering a key aimed at a dialog *above* it:
   * `Alt+W` on a dirty buffer opens the close confirmation over an open palette,
   * and the native window close does the same with no click at all. Only the
   * top layer acts (`overlays.ts`).
   *
   * Only while `open`: during the exit window the overlay is still mounted, and
   * an Escape aimed at whatever is underneath must reach it.
   */
  useOverlayKeys(open, (event: KeyboardEvent): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      dismiss();
      return;
    }
    if (event.key !== "Tab") return;
    const dialog = dialogRef.current;
    if (dialog === null) return;
    event.preventDefault();
    const stops = [...dialog.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (stops.length === 0) return;
    const at = stops.indexOf(document.activeElement as HTMLElement);
    // Focus that is already outside comes back to the near end rather than
    // stepping from a position in a ring it is not in.
    const next = at < 0 ? 0 : (at + (event.shiftKey ? -1 : 1) + stops.length) % stops.length;
    stops[next]?.focus();
  });

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
  /**
   * Run a row and get out of the way.
   *
   * Deliberately *not* `dismiss`: the command owns the keyboard from here. "New
   * terminal" focuses the terminal it opened, "Open file" focuses the editor —
   * restoring the invoker would yank focus straight back off whatever the user
   * just asked for. Cancelling is the case where the user goes back to where
   * they were; running is the case where they asked to be somewhere else.
   */
  const choose = (row: Row): void => {
    invokerRef.current = null;
    row.run();
    useStore.getState().setQuickBarOpen(false);
  };
  const exiting = leaving ? " is-leaving" : "";

  // Three modes, one surface. A `quickPick` is a list some capability supplied
  // (`store.ts`); it wins, because it was opened *for* that list — the `>`
  // prefix is a file/command toggle and has no meaning inside one.
  const actionsMode = pick === null && query.startsWith(">");
  let rows: Row[];
  // What the rows were matched against, kept for the highlight below — the `>`
  // is a mode switch and never a character anyone searched for.
  let matched: string;
  if (pick !== null) {
    const q = query.trim();
    matched = q;
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
    matched = q;
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
    matched = q;
    rows = files
      .map((path) => ({ path, match: fuzzyMatch(q, path) }))
      .filter((x) => x.match !== null)
      .sort((a, b) => (b.match?.score ?? 0) - (a.match?.score ?? 0))
      .slice(0, 50)
      .map(({ path, match }) => {
        // The row is ranked on the whole path and shown as a name beside its
        // folder, so the match is *split* rather than re-guessed from either
        // half: `ui/src/quick` is a real match for `ui/src/panels/QuickBar.tsx`
        // and matches neither label on its own, which is exactly the query a
        // file search is made of. `cut` is the index the basename starts at.
        const slash = path.lastIndexOf("/");
        const cut = slash + 1;
        return {
          key: path,
          title: path.slice(cut),
          detail: slash < 0 ? "" : path.slice(0, slash),
          titleHits: (match?.hits ?? []).filter((i) => i >= cut).map((i) => i - cut),
          detailHits: (match?.hits ?? []).filter((i) => i < slash),
          run: () => void useStore.getState().openFile(path),
        };
      });
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
  // The popup is "expanded" exactly when there is a listbox to expand into. An
  // empty result is a *collapsed* combobox with a message where the list was —
  // not a listbox with nothing in it, which is what the markup used to say.
  const expanded = rows.length > 0;

  /**
   * Consecutive rows of one category, which is what a section already was.
   *
   * A `listbox` may own only `option` and `group` children, so the category
   * headers — loose `div`s among the rows until now — become the label of a
   * real group. Nothing moves: the header is still the first thing in its
   * section and still `position: sticky`, only now inside its own group rather
   * than the whole list, which is where a sticky header should stop anyway.
   */
  const sections: { category: string | undefined; rows: { row: Row; index: number }[] }[] = [];
  rows.forEach((row, index) => {
    const last = sections[sections.length - 1];
    if (last !== undefined && last.category === row.category) last.rows.push({ row, index });
    else sections.push({ category: row.category, rows: [{ row, index }] });
  });

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
      if (row && row.disabled !== true) choose(row);
    }
    // Escape is deliberately absent: it is handled at the window in the capture
    // phase (see the effect above), which is what makes it work when focus is
    // anywhere but here. That listener stops propagation, so this handler would
    // never see the key even if it wanted to.
  };

  /** One result row: an `option` in the listbox, addressed by id, never a tab
   * stop. It stays a `<button>` — for the click, for `:disabled`, and for every
   * selector in `quickbar.css` that draws it. */
  const renderRow = ({ row, index }: { row: Row; index: number }) => (
    <button
      key={row.key}
      type="button"
      role="option"
      id={optionId(index)}
      aria-selected={index === selIdx}
      tabIndex={-1}
      disabled={row.disabled === true}
      ref={index === selIdx ? selRef : undefined}
      className={"wb-qb-row" + (index === selIdx ? " is-selected" : "")}
      onClick={() => choose(row)}
      onMouseMove={() => {
        if (row.disabled !== true) setSel(index);
      }}
    >
      <Labelled
        className="wb-qb-row-title"
        text={row.title}
        query={matched}
        positions={row.titleHits}
      />
      <Labelled
        className="wb-qb-row-detail"
        text={row.detail}
        query={matched}
        positions={row.detailHits}
      />
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
  );

  return (
    <>
      <div className={"wb-qb-backdrop" + exiting} onClick={dismiss} />
      {/* `aria-modal` is what tells assistive tech the window behind the scrim
          is not there — the visual half (60% black, `z-index` above the dock)
          always said so, the accessibility tree did not. The keyboard half is
          the Tab ring in the effect above; the two together are what make this
          a dialog rather than a floating div.

          No `aria-hidden` while it leaves: the input inside may still hold
          focus for those few frames, and hiding a focused subtree from the
          accessibility tree is worse than describing a dialog that is fading.
          `pointer-events: none` in the stylesheet is what makes it inert. */}
      <div
        ref={dialogRef}
        className={"wb-qb" + exiting}
        role="dialog"
        aria-modal="true"
        aria-label={pick?.label ?? "Quick open"}
      >
        {/* The ARIA combobox pattern: focus never leaves this input, and the
            option it is "on" is named by `aria-activedescendant`. That is the
            whole of the second finding — the amber wash said which row Enter
            would run to a pair of eyes and to nothing else. */}
        <input
          ref={inputRef}
          className="wb-qb-input"
          role="combobox"
          aria-label={pick?.label ?? "Quick open"}
          aria-autocomplete="list"
          aria-controls={LISTBOX_ID}
          aria-expanded={expanded}
          {...(expanded ? { "aria-activedescendant": optionId(selIdx) } : {})}
          placeholder={pick?.placeholder ?? "Search files — type > for actions"}
          value={query}
          spellCheck={false}
          onChange={(e) => {
            setQuery(e.target.value);
            setSel(0);
          }}
          onKeyDown={onKeyDown}
        />
        <div
          className="wb-qb-results"
          id={LISTBOX_ID}
          {...(expanded ? { role: "listbox", "aria-label": "Results" } : {})}
        >
          {rows.length === 0 && (
            <div className="wb-qb-empty">
              {pick !== null
                ? "Nothing matches"
                : actionsMode
                  ? "No matching actions"
                  : "No matching files"}
            </div>
          )}
          {sections.map((section, i) =>
            section.category === undefined ? (
              <Fragment key={`§${String(i)}`}>{section.rows.map(renderRow)}</Fragment>
            ) : (
              // The header is `aria-hidden` because the group is already
              // labelled with the same words — otherwise a reader hears
              // "Panes" twice before every row in the section.
              <div key={`§${String(i)}`} role="group" aria-label={section.category}>
                <div className="wb-qb-cat" aria-hidden="true">
                  {section.category}
                </div>
                {section.rows.map(renderRow)}
              </div>
            ),
          )}
        </div>
        <ResultCount count={rows.length} />
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
