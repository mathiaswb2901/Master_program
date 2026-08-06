/**
 * Discoverability, as a registered capability: **the app telling you what it
 * can do.**
 *
 * The change request behind it is one sentence from the owner after a day of
 * features landing — *"I am not sure how to use these features."* That is a
 * product failure, not a user failure: we shipped a keyboard-driven window with
 * no welcome, no shortcut reference and no hint that `Alt+S` splits a pane. A
 * capability nobody can find does not exist.
 *
 * One tool, because it is one idea, and it contributes three surfaces:
 *
 *  - **the keyboard reference** — a panel listing every command the registry
 *    knows, grouped by the tool that owns it, searchable, with keycaps
 *    (DESIGN.md §6.5) and the pass-through rule in plain words. Generated, never
 *    written down: `keyref.ts` derives it and `keyref.test.ts` fails the build
 *    if a registered command with a chord is unreachable from it;
 *  - **the welcome card** — the top of that same panel while a workspace has
 *    never dismissed it, and the panel opens itself on a window nobody has
 *    arranged yet. Four things worth trying, each a **real affordance that runs
 *    the command** rather than prose describing it. It is a tab, so it is never
 *    in the way: click any other tab and it is gone;
 *  - **the Keys chip** — the one permanent, mouse-reachable entry point in the
 *    status bar, for the user who dismissed the welcome a month ago and has
 *    forgotten the chord.
 *
 * What it deliberately is **not**: a tour, a modal, or a second overlay
 * language competing with the QuickBar. Nothing here has to be dismissed before
 * the app can be used.
 *
 * ## Where the dismissal lives, and why it is not `localStorage`
 *
 * `<workspace>/.workbench/welcome.json`, written through the ordinary files API
 * exactly as the Scratchpad writes its notes — no new endpoint, and the same
 * place every other piece of *window* state already lives (`layouts.json` is
 * its neighbour). Three consequences that `localStorage` would get wrong: the
 * shell and a browser tab agree, clearing browser storage does not bring the
 * scaffolding back, and opening a **new project** is a new window, which is
 * where a welcome belongs. Unreadable or malformed content counts as dismissed:
 * the failure direction that never nags.
 */

import type { DockviewApi, IDockviewPanelProps } from "dockview";
import { useEffect, useState } from "react";
import { create } from "zustand";

import * as api from "../api";
import { allCommands, type Command } from "../commands";
import { openPanel } from "../dock";
import {
  chordFor,
  filterKeyReference,
  keyReference,
  rowCount,
  type KeyRefRow,
} from "../keyref";
import { chordKeycaps } from "../keys";
import type { ToolCommand, WorkbenchTool } from "../registry";
import { useStore } from "../store";
import { TOOLS } from "../tools";

import "../styles/keyboard.css";

/** Stable contract: saved layouts reference this panel by it (`docs/tools.md`). */
const TOOL_ID = "keys";

const WELCOME_FILE = ".workbench/welcome.json";

// ---- the welcome's one bit of state ----------------------------------------

interface WelcomeState {
  /** `null` until the workspace has answered — the panel renders no welcome and
   * no absence of one until then, so a dismissed window never flashes it. */
  dismissed: boolean | null;
}

/**
 * This tool's own store. zustand remains the only state library and
 * `ui/src/store.ts` remains the home for app-wide state; nothing outside this
 * module reads whether the welcome has been dismissed, so it lives here
 * (CLAUDE.md, `docs/tools.md` "State").
 */
const useWelcome = create<WelcomeState>()(() => ({ dismissed: null }));

/**
 * The flag, out of the file's bytes.
 *
 * Present but unreadable still means "this window has been here before" —
 * truncated by a crash mid-write, hand-edited into invalid JSON, or carrying a
 * shape some later schema stopped writing. All of them are a workspace that has
 * been used, and the failure direction that never nags is *dismissed*.
 */
function parseDismissed(content: string): boolean {
  try {
    const parsed: unknown = JSON.parse(content);
    return (parsed as { dismissed?: unknown }).dismissed !== false;
  } catch {
    return true;
  }
}

/**
 * Has this workspace dismissed the welcome?
 *
 * The two failure modes here are not the same answer, and collapsing them is
 * how a discovery surface becomes a nag. **Only a genuine 404 means "never
 * dismissed"** — that is first run, and it is the whole of first run. Every
 * other failure (a 500, a permission error, a backend that is not up yet, a
 * file we could read but not parse) is *not* evidence that this window is new,
 * so it counts as dismissed: one bad `GET` during launch must never reopen the
 * welcome card on a workspace somebody has been using for a month.
 */
async function readDismissed(): Promise<boolean> {
  try {
    return parseDismissed((await api.getFileContent(WELCOME_FILE)).content);
  } catch (error) {
    return !(error instanceof api.ApiError && error.status === 404);
  }
}

/** Persist the dismissal. A write that fails costs the user one more welcome
 * on the next launch, which is not worth a toast. */
async function writeDismissed(dismissed: boolean): Promise<void> {
  try {
    await api.putFileContent({
      path: WELCOME_FILE,
      content: `${JSON.stringify({ dismissed }, null, 2)}\n`,
    });
  } catch {
    // storage unavailable — the welcome just comes back next time
  }
}

/** The welcome is over. Called by every path out of it: the four affordances,
 * the explicit dismissal, and the QuickBar command that hides it. */
function dismissWelcome(): void {
  useWelcome.setState({ dismissed: true });
  void writeDismissed(true);
}

function showWelcome(): void {
  useWelcome.setState({ dismissed: false });
  void writeDismissed(false);
  openPanel(TOOL_ID);
}

/**
 * Open the panel on a window that has never been arranged and never dismissed
 * the welcome.
 *
 * The second condition is what makes this **deterministic** rather than a race
 * with the layout system. Both this and `Layouts.tsx` start with a request:
 * theirs restores the arrangement with `fromJSON`, which removes any panel the
 * saved arrangement does not name — including one this function had just added.
 * So the rule is the one that cannot collide: if a saved arrangement exists, it
 * is the truth about which panels are open and this does nothing; if it does
 * not, nothing will restore over us. A window the user has arranged but never
 * seen the welcome in can still ask for it — "Show the welcome" is a command.
 *
 * The layouts request is made only on a workspace that has *not* dismissed the
 * welcome, so a returning user pays for one small `GET` of a file that is
 * usually a 404 and nothing else.
 */
async function openIfFirstRun(): Promise<void> {
  const dismissed = await readDismissed();
  useWelcome.setState({ dismissed });
  if (dismissed) return;
  try {
    if ((await api.getLayouts()).state.current !== null) return;
  } catch {
    // Layouts unavailable: the arrangement will not be restored either, so
    // there is nothing that can remove the panel we are about to open.
  }
  openPanel(TOOL_ID);
}

function onDockReady(dock: DockviewApi | null): void {
  if (dock === null) {
    useWelcome.setState({ dismissed: null });
    return;
  }
  void openIfFirstRun();
}

// ---- glyphs -----------------------------------------------------------------

function KeyIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="1.2" y="3.6" width="13.6" height="8.8" rx="1.4" />
      <path d="M4 6.4h.01M6.4 6.4h.01M8.8 6.4h.01M11.2 6.4h.01M4.8 9.6h6.4" />
    </svg>
  );
}

function ChipIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect x="1.2" y="3.6" width="13.6" height="8.8" rx="1.4" />
      <path d="M4.8 9.6h6.4" />
    </svg>
  );
}

// ---- keycaps ----------------------------------------------------------------

/** One chord, as keycaps (DESIGN.md §6.5). The labels come from `keys.ts`, so
 * `PageDown` reads `PgDn` here exactly as it does in the QuickBar. */
function Chord({ text }: { text: string }) {
  return (
    <span className="wb-keys-chord">
      {chordKeycaps(text).map((cap, index) => (
        // Positional key: a chord can repeat a cap ("Alt+Shift+Alt" cannot
        // happen, but nothing in the type stops it) and React must not care.
        <span key={`${cap}:${String(index)}`} className="wb-keycap">
          {cap}
        </span>
      ))}
    </span>
  );
}

// ---- the welcome card -------------------------------------------------------

interface WelcomeStep {
  /** The command this row runs — its chord and its existence both come from
   * the registry, so a row for a capability that is gated off disappears. */
  commandId: string;
  label: string;
  hint: string;
}

/**
 * Four things worth doing first, in the order a new user meets them: find
 * something, rearrange the window, put an agent to work, see everything else.
 *
 * The labels are prose because a welcome speaks plainly ("Split this pane in
 * two", not "Split this pane to the right…"); the **chords are not** — they are
 * read from the registry on every render, so a tool that re-binds one re-labels
 * this card without anybody remembering to.
 */
const WELCOME_ACTIONS: readonly WelcomeStep[] = [
  {
    commandId: "quickbar.files",
    label: "Find a file",
    hint: "every file in the workspace, as you type",
  },
  {
    commandId: "pane.split.right",
    label: "Split this pane in two",
    hint: "then pick what goes in it — any tool, any session",
  },
  { commandId: "session.new", label: "Put an agent to work", hint: "a Claude session in this folder" },
  {
    commandId: "quickbar.commands",
    label: "See every command",
    hint: "the whole app, from one box",
  },
];

function WelcomeAction({ action, command }: { action: WelcomeStep; command: Command }) {
  const chord = command.keys?.[0];
  return (
    <button
      type="button"
      className="wb-welcome-action"
      onClick={() => {
        // Do the thing, then get out of the way for good: a user who has
        // started does not need to be told how to start.
        dismissWelcome();
        command.run();
      }}
    >
      <span className="wb-welcome-action-text">
        <span className="wb-welcome-action-label">{action.label}</span>
        <span className="wb-welcome-action-hint">{action.hint}</span>
      </span>
      {chord !== undefined && <Chord text={chord} />}
    </button>
  );
}

function WelcomeCard() {
  const commands = allCommands();
  // A row for a command that is not registered (a capability gated off on this
  // machine) is simply not offered — never a button that does nothing.
  const actions = WELCOME_ACTIONS.flatMap((action) => {
    const command = commands.find((candidate) => candidate.id === action.commandId);
    return command === undefined ? [] : [{ action, command }];
  });

  return (
    <section className="wb-welcome" aria-label="Welcome to Workbench">
      <h2 className="wb-welcome-title">Welcome to Workbench</h2>
      <p className="wb-welcome-lede">
        One window for your files, your documents, your terminals and your agents. Every pane
        splits, every tool goes in any pane, and everything here has a keyboard path.
      </p>
      <div className="wb-welcome-actions">
        {actions.map(({ action, command }) => (
          <WelcomeAction key={action.commandId} action={action} command={command} />
        ))}
      </div>
      <div className="wb-welcome-footer">
        <span className="wb-welcome-note">
          Below: every shortcut this window knows. It is always here — {chordFor("keys.open")}.
        </span>
        <button type="button" className="wb-btn wb-btn-sm wb-btn-ghost" onClick={dismissWelcome}>
          Got it
        </button>
      </div>
    </section>
  );
}

// ---- the reference ----------------------------------------------------------

/**
 * The pass-through rule, in the words a user needs (DESIGN.md §6.8).
 *
 * Prose, and deliberately: this is the one thing on the surface that is not a
 * command, and it is the answer to the question the keymap raises the first
 * time `Ctrl+P` reaches a shell instead of the app.
 */
function PassThrough() {
  return (
    <section className="wb-keys-note" aria-label="Why some chords reach the terminal">
      <div className="wb-keys-note-title u-label">Why some chords do not reach Workbench</div>
      <p>
        A terminal and a code editor are keyboard applications of their own: inside them{" "}
        <span className="wb-keycap">Ctrl</span>
        <span className="wb-keycap">K</span> kills a line and{" "}
        <span className="wb-keycap">Ctrl</span>
        <span className="wb-keycap">P</span> walks shell history, so Workbench leaves them alone
        and takes only chords carrying <span className="wb-keycap">Alt</span> or{" "}
        <span className="wb-keycap">Ctrl</span>
        <span className="wb-keycap">Shift</span>. That is why{" "}
        <span className="wb-keycap">Ctrl</span>
        <span className="wb-keycap">Shift</span>
        <span className="wb-keycap">P</span> opens the command palette from anywhere while{" "}
        <span className="wb-keycap">Ctrl</span>
        <span className="wb-keycap">P</span> does so only outside a terminal, and why every
        chord worth a reflex here is an <span className="wb-keycap">Alt</span> one.
      </p>
      <p>
        Plain keys are never intercepted — typing always reaches what you are typing into. Your
        own shortcuts (<code>.workbench/shortcuts.md</code>) carry{" "}
        <span className="wb-keycap">Alt</span> for the same reason.
      </p>
    </section>
  );
}

/** What the detail slot says for a chord that would do nothing if pressed now.
 * Plain, and it replaces the live detail rather than sitting beside it: a
 * gated-off command's `detail()` is usually empty anyway (there is no session
 * to name), and "the chord is inert" is the fact worth the column. */
const UNAVAILABLE_DETAIL = "not available yet";

function ReferenceRow({ row }: { row: KeyRefRow }) {
  return (
    <div className={`wb-keys-row${row.available ? "" : " is-unavailable"}`}>
      <span className="wb-keys-row-title u-truncate">{row.title}</span>
      <span className="wb-keys-row-detail u-truncate">
        {row.available ? row.detail : UNAVAILABLE_DETAIL}
      </span>
      <span className="wb-keys-row-chords">
        {row.chords.map((chord) => (
          <Chord key={chord} text={chord} />
        ))}
      </span>
    </div>
  );
}

// `panel`, not `api`: this module's `api` is the HTTP client above.
function KeyboardPanel({ api: panel }: IDockviewPanelProps) {
  const dismissed = useWelcome((s) => s.dismissed);
  const [query, setQuery] = useState("");
  // Availability is a *live* read (`Command.when`), and this panel is a tab
  // that stays mounted behind whatever you do next — so the answer it showed
  // when you opened it goes stale the moment you start a session. Re-render
  // when the tab is brought forward, which is the only moment the rows are
  // being read. Nothing else about the reference changes on activation, so
  // this is one render, not a subscription to the app's state.
  const [, revisit] = useState(0);
  useEffect(() => {
    const subscription = panel.onDidActiveChange((event) => {
      if (event.isActive) revisit((n) => n + 1);
    });
    return () => subscription.dispose();
  }, [panel]);
  // Subscribed to the one input that changes this list while the panel is open:
  // the user editing `.workbench/shortcuts.md`, which the watcher pushes back
  // as new commands. Their rows appear here without a reload — which is the
  // whole claim of a *generated* reference, made where a user can see it.
  useStore((s) => s.shortcuts);
  // Every command in the app: the built-ins, whatever a tool contributes at
  // runtime (one row per saved layout) and the user's own entries. Memoized
  // upstream, so this is an identity read rather than a rebuild.
  const commands = allCommands();
  // Deliberately unmemoized: `keyReference` asks every command whether it would
  // run right now, and a memo keyed on the (stable) command list would cache
  // that answer for the life of the panel. It is a map over ~60 commands.
  const groups = keyReference(TOOLS, commands);
  const shown = filterKeyReference(groups, query);
  const total = rowCount(shown);

  // The welcome is the first thing on a first-run window, so the search box is
  // not stealing focus from it. Once it is gone, this panel is a reference and
  // the search box is what you came for.
  const searchAutoFocus = dismissed === true;

  return (
    <div className="wb-keys">
      {dismissed === false && <WelcomeCard />}
      <div className="wb-keys-search">
        <input
          className="wb-keys-input"
          type="search"
          autoFocus={searchAutoFocus}
          value={query}
          spellCheck={false}
          aria-label="Search shortcuts"
          placeholder="Search commands and chords — try “split” or “alt+s”"
          onChange={(event) => setQuery(event.target.value)}
        />
      </div>
      <div className="wb-keys-body">
        {total === 0 && (
          <div className="wb-keys-empty">
            No command matches “{query.trim()}”. Every command is here, so try a word from what
            you want to do — “session”, “layout”, “pane”.
          </div>
        )}
        {shown.map((group) => (
          <section key={group.id} className="wb-keys-group" aria-label={group.title}>
            <div className="wb-keys-group-title u-label">{group.title}</div>
            {group.rows.map((row) => (
              <ReferenceRow key={row.id} row={row} />
            ))}
          </section>
        ))}
        <PassThrough />
      </div>
    </div>
  );
}

// ---- the status chip --------------------------------------------------------

/**
 * Status bar, right region: the one permanent way in.
 *
 * The welcome is gone once you have used the window, and the QuickBar is itself
 * a thing you have to know about — so a keyboard-first app owes the mouse one
 * visible entry point to its keyboard. It is the layout chip's neighbour and
 * its anatomy (DESIGN.md §6.7/§6.9), and its tooltip names the chord, which is
 * how the mouse path teaches the keyboard one.
 */
function KeysStatus() {
  return (
    <button
      type="button"
      className="wb-status-chip wb-keys-chip"
      title={`Keyboard shortcuts — ${chordFor("keys.open")}`}
      onClick={() => openPanel(TOOL_ID)}
    >
      <ChipIcon />
      <span className="wb-keys-chip-label">Keys</span>
    </button>
  );
}

// ---- registration -----------------------------------------------------------

const commands: readonly ToolCommand[] = [
  {
    id: "keys.open",
    title: "Keyboard shortcuts",
    detail: () => "every command, grouped by tool",
    run: () => openPanel(TOOL_ID),
  },
  {
    id: "keys.welcome",
    title: "Show the welcome card",
    detail: () => "what this window is, and four things to try",
    run: showWelcome,
  },
];

export const keyboardTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Keyboard",
  icon: KeyIcon,
  panel: {
    component: KeyboardPanel,
    // Centre, so it opens as a tab in the pane the window is built around — the
    // VS Code "Get Started" position, and the reason the welcome interrupts
    // nothing: another tab is one click away and the panel closes for good.
    defaultLocation: { area: "center" },
    openByDefault: false,
    // One reference is enough, and two would be two of the same list.
    singleton: true,
  },
  commands,
  // `Alt+K` is the one chord this tool takes, and the bar for taking one is "a
  // reflex, not a lookup" (`docs/tools.md`). It clears it by being the chord
  // that answers *what are the chords* — the only one a user needs before they
  // know any others — and `Alt` is what reaches Workbench from inside a
  // terminal or an editor, which is exactly where you are when you have
  // forgotten one. It is advertised on the status chip, on the welcome card and
  // on its own QuickBar row, so it is discoverable three ways.
  shortcuts: { "keys.open": ["Alt+K"] },
  statusContributions: [{ region: "right", component: KeysStatus }],
  onDockReady,
};
