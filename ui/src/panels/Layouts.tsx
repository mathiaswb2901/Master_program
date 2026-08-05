/**
 * The layout system, as a registered capability.
 *
 * It contributes no panel — it operates on the dock rather than living in it —
 * which is exactly why it is a tool and not another branch in `App.tsx`: focus
 * mode, layout persistence and named layouts arrive as commands, a status item,
 * a `shortcuts.md` kind and one `onDockReady` hook, and `tools.ts` gains one
 * line. Nothing else in the app knows layouts exist.
 *
 * Three behaviours live here:
 *
 *  - **Focus mode** (`Alt+M`): dockview's `maximizeGroup` / `exitMaximizedGroup`
 *    on the focused panel — the "work full screen" gap. Restoring is dockview's
 *    own hidden-view bookkeeping, so the arrangement comes back exactly.
 *  - **Persistence**: the live arrangement is debounced into
 *    `<workspace>/.workbench/layouts.json` and restored on the next load. Per
 *    workspace, because the file is *in* the workspace.
 *  - **Named layouts**: presets built from the registry plus whatever the user
 *    saved, switchable from the QuickBar, from this status chip, and from a
 *    `shortcuts.md` `layout` entry.
 *
 * Every layout that comes off disk is pruned first (`../layouts.ts`): dockview
 * will happily restore a panel whose component nothing is registered under, and
 * React then dies on an `undefined` element type. One stale entry must cost that
 * panel, never the window.
 */

import type { DockviewApi, DockviewIDisposable } from "dockview";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { create } from "zustand";

import * as api from "../api";
import { dockApiHandle } from "../dock";
import {
  DEFAULT_LAYOUT_NAME,
  LAYOUT_PRESETS,
  MAX_LAYOUT_NAME_CHARS,
  MAX_SAVED_LAYOUTS,
  droppedPanelsMessage,
  droppedPanesMessage,
  presetPlacements,
  pruneLayout,
  type LayoutPreset,
} from "../layouts";
import {
  applyPlacements,
  defaultLayout,
  paneVocabulary,
  type PaneVocabulary,
  type ToolCommand,
  type WorkbenchTool,
} from "../registry";
import { useStore } from "../store";
import { TOOLS } from "../tools";
import type { LayoutsResponse, LayoutsState, NamedLayout } from "../types";

import "../styles/layouts.css";

/** Long enough that a drag-resize is one write, short enough that a reload
 * seconds later still finds what you just did. */
const SAVE_DEBOUNCE_MS = 500;

/** QuickBar section for everything this tool contributes. */
export const LAYOUTS_CATEGORY = "Layouts";

/** Joins saved names into this tool's `dynamicCommands.key`. A byte no layout
 * name can hold, so two names cannot run together into a third set's key. */
const LAYOUT_KEY_SEPARATOR = "\u0000";

// ---- state ------------------------------------------------------------------

interface LayoutUiState {
  /** Named layouts, as persisted. The QuickBar rows and the menu read this. */
  saved: NamedLayout[];
  /** The layout the user last switched to, for the status chip. Advisory: it
   * is not cleared when they drag a panel, because "Review, adjusted" is still
   * Review — and a chip that flickers to "Custom" on every sash drag would be
   * noise where the whole point is a quiet bar. */
  current: string | null;
  maximized: boolean;
  menu: "closed" | "list" | "save";
}

/**
 * The capability's own store. zustand remains the only state library in the app
 * and `useStore` remains the home for app-wide state; this is a second
 * *instance* on purpose — layout state is this tool's and nothing else reads
 * it, so putting it in the app store would put a capability back into a shared
 * file that parallel lanes edit. That is now the written standard rather than
 * this tool's local argument (CLAUDE.md, `docs/tools.md` "State"). `useStore` is
 * still reached for the things that genuinely are app-wide: toasts.
 */
const useLayoutUi = create<LayoutUiState>()(() => ({
  saved: [],
  current: null,
  maximized: false,
  menu: "closed",
}));

/** Armed only once a restore has settled: a read that failed, or has not
 * happened yet, must never let the default arrangement overwrite the file. */
let persistenceArmed = false;
let saveTimer: number | undefined;
let saveFailureReported = false;
let dockListeners: DockviewIDisposable[] = [];

const toast = (kind: "error" | "warn" | "info" | "success", message: string): void =>
  useStore.getState().pushToast(kind, message);

const detailOf = (err: unknown): string =>
  err instanceof api.ApiError ? err.detail : err instanceof Error ? err.message : String(err);

/** What a pane id may say right now: the components the app can render, and
 * which of them may carry an instance key. A registry fact, read fresh each
 * time, and the set a restored layout is vetted against (`../panes.ts`). */
const knownPanes = (): PaneVocabulary => paneVocabulary(TOOLS);

// ---- persistence ------------------------------------------------------------

function layoutsDocument(dock: DockviewApi): LayoutsState {
  const state = useLayoutUi.getState();
  return { current: dock.toJSON(), current_name: state.current, saved: state.saved };
}

/** Tail of the write chain — see `persist`. Settles fulfilled, always. */
let writeChain: Promise<void> = Promise.resolve();
/** A write is queued and has not yet read the document it is going to send. */
let writeQueued = false;

/** One request. Reads the document at the moment it is sent, never earlier. */
async function writeOnce(): Promise<void> {
  // Cleared before the read below, with no `await` in between: from here on a
  // change needs its own request, and everything up to here rides this one.
  writeQueued = false;
  const dock = dockApiHandle();
  if (dock === null) return;
  try {
    await api.putLayouts(layoutsDocument(dock));
    saveFailureReported = false;
  } catch (err) {
    // Once per outage, not once per drag: this runs on every layout change.
    if (saveFailureReported) return;
    saveFailureReported = true;
    toast("error", `Layout not saved: ${detailOf(err)}`);
  }
}

/**
 * Write the whole layouts document — one request at a time, newest state wins.
 *
 * `PUT /api/layouts` replaces the document and the server has no ordering
 * authority: it `os.replace()`s whatever arrives, so two requests in flight at
 * once persist in delivery order, not in the order the user acted. Switch to
 * Review, switch to Agents, let the network deliver those two responses the
 * other way round, and disk is left holding *Review* while the window and the
 * chip both say Agents — the user's actual choice lost silently, and handed
 * back to them on the next reload. `SAVE_DEBOUNCE_MS` does not cover this: it
 * coalesces the continuous events of a drag, while two deliberate actions a
 * moment apart each write immediately and independently.
 *
 * So every write goes through one chain, and the body is read when the request
 * is about to be sent rather than when it was asked for. A write waiting behind
 * another therefore sends the arrangement as it is *now* — which is what makes
 * a second queued write redundant, so `writeQueued` collapses them instead of
 * letting the chain grow one link per keystroke.
 */
function persist(): Promise<void> {
  if (writeQueued) return writeChain;
  writeQueued = true;
  // `catch` on the chain and not only inside the write: a chain left rejected
  // would swallow every write after it, and losing persistence silently is the
  // exact failure this function exists to prevent.
  writeChain = writeChain.then(writeOnce).catch(() => undefined);
  return writeChain;
}

function scheduleSave(): void {
  if (!persistenceArmed) return;
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(() => void persist(), SAVE_DEBOUNCE_MS);
}

// ---- applying an arrangement ------------------------------------------------

/** Back to the registry's own layout, from any state (including maximized). */
function applyDefault(dock: DockviewApi): void {
  if (dock.hasMaximizedGroup()) dock.exitMaximizedGroup();
  dock.clear();
  applyPlacements(dock, defaultLayout(TOOLS));
}

/**
 * What applying an arrangement did to the dock.
 *
 * The two failure modes are not interchangeable, which is why this is not a
 * boolean. `"unchanged"` means the window is exactly as it was, so whatever
 * name it already carries is still true. `"default"` means the window is now
 * the registry's default arrangement — which is *not* what the caller asked
 * for, and is nobody's named layout. A caller that goes on calling it by the
 * requested name puts the wrong label on the status chip, and, because every
 * switch is persisted, writes that wrong label to `layouts.json` under the
 * arrangement it does not describe.
 */
type ApplyOutcome = "applied" | "unchanged" | "default";

/** Apply a serialized arrangement, having vetted it. See `ApplyOutcome`. */
function applySerialized(dock: DockviewApi, state: unknown, source: string): ApplyOutcome {
  if (state === null || state === undefined) return "unchanged";
  const pruned = pruneLayout(state, knownPanes());
  if (pruned === null) {
    // Nothing was touched, so say so: on startup the window this leaves behind
    // is the default arrangement, but mid-session it is whatever the user was
    // already in, and "using the default" would be a lie about half of them.
    toast(
      "warn",
      `${source} no longer describes any panel this Workbench has — leaving the window as it is.`,
    );
    return "unchanged";
  }
  try {
    // `reuseExistingPanels`: a panel that exists in both arrangements is moved,
    // not recreated — so a restore does not tear down Monaco or a live PTY.
    dock.fromJSON(pruned.layout, { reuseExistingPanels: true });
  } catch (err) {
    // dockview strips the half-built layout on a failed deserialize — it calls
    // `clear()` *before* it validates the grid — which would leave an empty
    // window. The default arrangement is the floor. `pruneLayout` vets panel
    // ids, not dockview's grid algebra, so this is reachable with a file whose
    // every panel is registered.
    applyDefault(dock);
    toast("error", `${source} could not be restored (${detailOf(err)}) — using the default.`);
    return "default";
  }
  if (pruned.dropped.length > 0) toast("warn", droppedPanelsMessage(pruned.dropped));
  if (pruned.droppedPanes.length > 0) toast("warn", droppedPanesMessage(pruned.droppedPanes));
  return "applied";
}

/** Build a preset out of whatever is registered. False = nothing it names
 * exists any more, in which case the window is deliberately left alone. */
function applyPreset(dock: DockviewApi, preset: LayoutPreset): boolean {
  const placements = presetPlacements(TOOLS, preset);
  if (placements.length === 0) {
    toast("warn", `The ${preset.name} layout needs panels this Workbench does not have.`);
    return false;
  }
  if (dock.hasMaximizedGroup()) dock.exitMaximizedGroup();
  dock.clear();
  applyPlacements(dock, placements);
  if (preset.maximize !== undefined) {
    const panel = dock.getPanel(preset.maximize);
    // A preset that asks to maximize a panel it no longer has is not an error:
    // the arrangement still applies, it just is not full screen.
    if (panel !== undefined) dock.maximizeGroup(panel);
  }
  return true;
}

const matches = (a: string, b: string): boolean =>
  a.trim().toLowerCase() === b.trim().toLowerCase();

/** Switch to a layout by name — presets, the default, or one the user saved.
 * A name nobody has is a toast, never a broken window: this is reachable from
 * `shortcuts.md`, where the name is whatever the file happens to say. */
export function switchToLayout(name: string): void {
  const dock = dockApiHandle();
  if (dock === null) return;
  if (matches(name, DEFAULT_LAYOUT_NAME)) {
    applyDefault(dock);
    useLayoutUi.setState({ current: null });
    void persist();
    return;
  }
  const preset = LAYOUT_PRESETS.find((candidate) => matches(candidate.name, name));
  if (preset !== undefined) {
    // False = nothing the preset names is registered and the dock was never
    // touched. The arrangement, and the name it already carries, both stand —
    // so there is nothing to relabel and nothing new to write.
    if (!applyPreset(dock, preset)) return;
    useLayoutUi.setState({ current: preset.name });
    void persist();
    return;
  }
  const saved = useLayoutUi.getState().saved.find((candidate) => matches(candidate.name, name));
  if (saved === undefined) {
    toast("warn", `No layout named "${name}".`);
    return;
  }
  const outcome = applySerialized(dock, saved.state, `The "${saved.name}" layout`);
  if (outcome === "unchanged") return;
  // `"default"` put the default arrangement on screen, which no saved name
  // describes — so the chip goes back to unnamed, exactly as `restore` does
  // for the same failure. Persisting `saved.name` over it would carry the
  // mislabelling to disk, where a reload would read it back as truth.
  useLayoutUi.setState({ current: outcome === "applied" ? saved.name : null });
  void persist();
}

export function saveCurrentLayout(rawName: string): void {
  const dock = dockApiHandle();
  const name = rawName.trim().slice(0, MAX_LAYOUT_NAME_CHARS);
  if (dock === null || name === "") return;
  if (
    matches(name, DEFAULT_LAYOUT_NAME) ||
    LAYOUT_PRESETS.some((preset) => matches(preset.name, name))
  ) {
    toast("warn", `"${name}" is a built-in layout — pick another name.`);
    return;
  }
  const others = useLayoutUi
    .getState()
    .saved.filter((layout) => !matches(layout.name, name));
  if (others.length >= MAX_SAVED_LAYOUTS) {
    toast("warn", `Only ${String(MAX_SAVED_LAYOUTS)} layouts can be saved — delete one first.`);
    return;
  }
  useLayoutUi.setState({ saved: [...others, { name, state: dock.toJSON() }], current: name });
  void persist();
  toast("success", `Layout "${name}" saved.`);
}

export function deleteLayout(name: string): void {
  const state = useLayoutUi.getState();
  if (!state.saved.some((layout) => matches(layout.name, name))) return;
  useLayoutUi.setState({
    saved: state.saved.filter((layout) => !matches(layout.name, name)),
    current: state.current !== null && matches(state.current, name) ? null : state.current,
  });
  void persist();
}

/** Focus mode: the focused panel fills the window, or gives the window back. */
export function toggleFocusMode(): void {
  const dock = dockApiHandle();
  if (dock === null) return;
  if (dock.hasMaximizedGroup()) {
    dock.exitMaximizedGroup();
    return;
  }
  const panel = dock.activePanel;
  if (panel === undefined) {
    toast("warn", "Nothing is focused — click a panel first.");
    return;
  }
  dock.maximizeGroup(panel);
}

// ---- startup ----------------------------------------------------------------

/** GET, surfacing the file-level problem as a toast. A corrupt or stale file
 * costs the arrangement and says so — it never costs the window. */
async function loadLayouts(): Promise<LayoutsResponse> {
  const response = await api.getLayouts();
  if (response.problem !== null) {
    toast("warn", `${response.problem} — started from the default layout.`);
  }
  return response;
}

async function restore(dock: DockviewApi): Promise<void> {
  let state: LayoutsState;
  try {
    state = (await loadLayouts()).state;
  } catch (err) {
    // We do not know what is on disk. Leaving persistence disarmed is the
    // conservative half: a failed read must not let this session's default
    // arrangement overwrite the arrangement the user actually has.
    toast("warn", `Layouts unavailable (${detailOf(err)}) — this session will not be saved.`);
    return;
  }
  useLayoutUi.setState({ saved: state.saved, current: state.current_name });
  if (applySerialized(dock, state.current, "Your saved layout") === "applied") {
    useLayoutUi.setState({ maximized: dock.hasMaximizedGroup() });
  } else {
    // Neither failure leaves the named arrangement on screen, so neither may
    // leave its name on the chip.
    useLayoutUi.setState({ current: null });
  }
  // Armed last, so nothing above is written back before it has been read.
  persistenceArmed = true;
}

function onDockReady(dock: DockviewApi | null): void {
  for (const listener of dockListeners) listener.dispose();
  dockListeners = [];
  window.clearTimeout(saveTimer);
  persistenceArmed = false;
  saveFailureReported = false;
  useLayoutUi.setState({ maximized: false, menu: "closed", current: null });
  if (dock === null) return;
  dockListeners = [
    dock.onDidLayoutChange(() => scheduleSave()),
    dock.onDidMaximizedGroupChange(() => {
      useLayoutUi.setState({ maximized: dock.hasMaximizedGroup() });
      scheduleSave();
    }),
  ];
  void restore(dock);
}

// ---- the status chip and its menu -------------------------------------------

function LayoutIcon() {
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
      <rect x="1.6" y="2.6" width="12.8" height="10.8" rx="1" />
      <path d="M6.4 2.6v10.8M6.4 8.6h8" />
    </svg>
  );
}

function LayoutMenu() {
  const saved = useLayoutUi((s) => s.saved);
  const mode = useLayoutUi((s) => s.menu);
  const [name, setName] = useState("");
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (mode === "save") input.current?.focus();
  }, [mode]);

  const close = (): void => useLayoutUi.setState({ menu: "closed" });
  const onSubmit = (event: FormEvent): void => {
    event.preventDefault();
    if (name.trim() === "") return;
    saveCurrentLayout(name);
    setName("");
    close();
  };

  return (
    <>
      <div className="wb-menu-backdrop" onClick={close} />
      <div className="wb-layout-menu" role="dialog" aria-label="Layouts">
        <div className="wb-layout-menu-label u-label">Layouts</div>
        {[DEFAULT_LAYOUT_NAME, ...LAYOUT_PRESETS.map((preset) => preset.name)].map((preset) => (
          <button
            key={preset}
            type="button"
            className="wb-menu-item"
            onClick={() => {
              switchToLayout(preset);
              close();
            }}
          >
            {preset}
          </button>
        ))}
        {saved.length > 0 && <div className="wb-layout-menu-label u-label">Saved</div>}
        {saved.map((layout) => (
          <div key={layout.name} className="wb-layout-row">
            <button
              type="button"
              className="wb-menu-item"
              onClick={() => {
                switchToLayout(layout.name);
                close();
              }}
            >
              {layout.name}
            </button>
            <button
              type="button"
              className="wb-layout-delete"
              aria-label={`Delete the ${layout.name} layout`}
              title={`Delete the ${layout.name} layout`}
              onClick={() => deleteLayout(layout.name)}
            >
              ×
            </button>
          </div>
        ))}
        <form className="wb-layout-save" onSubmit={onSubmit}>
          <input
            ref={input}
            className="wb-layout-save-input"
            aria-label="Name for this arrangement"
            placeholder="Save this arrangement as…"
            maxLength={MAX_LAYOUT_NAME_CHARS}
            spellCheck={false}
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") close();
            }}
          />
          <button
            type="submit"
            className="wb-btn wb-btn-sm wb-btn-outline"
            disabled={name.trim() === ""}
          >
            Save
          </button>
        </form>
      </div>
    </>
  );
}

/**
 * Status bar, right region: what arrangement you are in, and whether one panel
 * has the window (DESIGN.md §6.9). The whole layout system is reachable from
 * it by mouse, which is what keeps the QuickBar from being the only way in.
 */
function LayoutStatus() {
  const menu = useLayoutUi((s) => s.menu);
  const current = useLayoutUi((s) => s.current);
  const maximized = useLayoutUi((s) => s.maximized);
  const label = maximized ? "Focused" : (current ?? "Layout");
  return (
    <div className="wb-layout-status">
      {menu !== "closed" && <LayoutMenu />}
      <button
        type="button"
        className={"wb-status-chip wb-layout-chip" + (maximized ? " is-focused" : "")}
        aria-haspopup="dialog"
        aria-expanded={menu !== "closed"}
        title={
          maximized
            ? "Focus mode — Alt+M restores the arrangement"
            : "Layouts — save, switch, reset"
        }
        onClick={() =>
          useLayoutUi.setState({ menu: menu === "closed" ? "list" : "closed" })
        }
      >
        <LayoutIcon />
        <span className="wb-layout-chip-label u-truncate">{label}</span>
      </button>
    </div>
  );
}

// ---- registration -----------------------------------------------------------

/** One row per layout you can switch to, plus one per layout you can delete.
 * Dynamic because the saved set changes while the app runs — the registry
 * re-derives them when `key` changes and not otherwise (`registry.ts`). */
function layoutCommands(): ToolCommand[] {
  const saved = useLayoutUi.getState().saved;
  const switches: ToolCommand[] = [
    {
      id: "layout.apply.default",
      title: `Switch to the ${DEFAULT_LAYOUT_NAME} layout`,
      detail: () => "the startup arrangement",
      category: LAYOUTS_CATEGORY,
      run: () => switchToLayout(DEFAULT_LAYOUT_NAME),
    },
    ...LAYOUT_PRESETS.map((preset) => ({
      id: `layout.apply.${preset.id}`,
      title: `Switch to the ${preset.name} layout`,
      detail: () => preset.detail,
      category: LAYOUTS_CATEGORY,
      run: () => switchToLayout(preset.name),
    })),
    ...saved.map((layout) => ({
      id: `layout.apply.saved.${layout.name}`,
      title: `Switch to the ${layout.name} layout`,
      detail: () => "saved layout",
      category: LAYOUTS_CATEGORY,
      run: () => switchToLayout(layout.name),
    })),
  ];
  const deletions: ToolCommand[] = saved.map((layout) => ({
    id: `layout.delete.${layout.name}`,
    title: `Delete the ${layout.name} layout`,
    category: LAYOUTS_CATEGORY,
    run: () => deleteLayout(layout.name),
  }));
  return [...switches, ...deletions];
}

export const layoutsTool: WorkbenchTool = {
  id: "layouts",
  title: "Layouts",
  commands: [
    {
      id: "layout.focus",
      title: "Toggle focus mode (fill the window with this panel)",
      category: LAYOUTS_CATEGORY,
      run: toggleFocusMode,
    },
    {
      id: "layout.save",
      title: "Save this arrangement as a layout…",
      category: LAYOUTS_CATEGORY,
      run: () => useLayoutUi.setState({ menu: "save" }),
    },
  ],
  // `Alt+M` is the one chord this tool takes, and it earns it: working full
  // screen is a reflex, and Alt is the only modifier that reaches Workbench
  // from inside Monaco and xterm — where you are when you want it.
  shortcuts: { "layout.focus": ["Alt+M"] },
  dynamicCommands: {
    key: () => useLayoutUi.getState().saved.map((layout) => layout.name).join(LAYOUT_KEY_SEPARATOR),
    build: layoutCommands,
  },
  // The one `shortcuts.md` kind that acts instead of inserting. It can do
  // exactly one thing — move panels into an arrangement the user saved — which
  // is why it does not break "a workspace file may add rows, never actions".
  shortcutActions: { layout: switchToLayout },
  statusContributions: [{ region: "right", component: LayoutStatus }],
  onDockReady,
};
