/**
 * The pane system, as a registered capability.
 *
 * Workbench had four panels in four fixed places. This is the tool that takes
 * that arrangement away from the app and gives it to the user: **any pane
 * splits in two, anything registered goes in the new one, and the keyboard
 * moves between them.** tmux, translated — a window manager for the four
 * surfaces an analyst actually works in.
 *
 * Like the layout system it contributes **no panel of its own**; it operates on
 * the dock. What it does contribute is:
 *
 *  - **commands** for split / focus / swap / cycle / close, every one of them in
 *    the QuickBar with its keycap and bindable from `shortcuts.md`;
 *  - **the picker**: one row per registered tool and per thing a plural tool can
 *    be bound to (each live session, each open file, a new terminal), shown in
 *    the QuickBar itself rather than in a second overlay (`store.ts`'s
 *    `QuickPick`, DESIGN.md §6.5);
 *  - **the split affordance**: two glyphs on the focused pane's tab strip, via
 *    the registry's `groupActions` so `App.tsx` still names no capability
 *    (DESIGN.md §6.11).
 *
 * ## What a pane is, here
 *
 * A pane is one dockview **group** — a tab strip and the panels under it — and
 * that is what focus, swap, cycle and the split geometry all operate on. Close
 * is the exception and closes the focused *panel*, because a group holding
 * three editor tabs is one pane with three things in it, and "close this pane"
 * should not throw away two of them unasked.
 *
 * ## Identity, and why it is only ever a string
 *
 * See `ui/src/panes.ts`. Everything here places panes by **pane id**, and a
 * pane id that already exists is *moved* into the split rather than duplicated:
 * "put session X here" means the one pane showing X, wherever it was. That is
 * also what makes the operation idempotent for a singleton — picking `Files`
 * moves the Files pane into the split, which is exactly what a tiling window
 * manager does with a window you already have.
 */

import type {
  DockviewApi,
  DockviewGroupPanel,
  DockviewIDisposable,
  IDockviewHeaderActionsProps,
  IDockviewPanel,
} from "dockview";

import { dockApiHandle } from "../dock";
import { chordTooltip } from "../keyref";
import {
  cyclePane,
  oppositeDirection,
  paneId,
  paneInDirection,
  PANE_DIRECTIONS,
  type PaneDirection,
  type PaneRect,
} from "../panes";
import {
  paneInstanceOptions,
  paneTitle,
  popoutRefusal,
  type PaneChoice,
  type ToolCommand,
  type WorkbenchTool,
} from "../registry";
import { useStore, type QuickPickRow } from "../store";
import { documentTheme } from "../theme";
import { TOOLS } from "../tools";

import "../styles/panes.css";

/** QuickBar section for everything this tool contributes. */
export const PANES_CATEGORY = "Panes";

/** Which way a split puts the new pane. Two axes, as in tmux — the third
 * ("into this pane's tab strip") is what dragging a tab already does. */
export type SplitDirection = "right" | "down";

/**
 * Our directions in dockview's two vocabularies — which are not the same one.
 * `addPanel` takes a `Direction` (`above`/`below`), `moveTo` takes a `Position`
 * (`top`/`bottom`), and mixing them type-checks nowhere but reads as if it
 * should.
 */
const ADD_DIRECTION = {
  left: "left",
  right: "right",
  up: "above",
  down: "below",
} as const;

const MOVE_POSITION = {
  left: "left",
  right: "right",
  up: "top",
  down: "bottom",
} as const;

// ---- reading the dock -------------------------------------------------------

/**
 * Every pane on screen, as boxes.
 *
 * Zero-sized groups are left out on purpose: in focus mode (`Alt+M`) dockview
 * keeps the other groups alive at 0×0, and a directional move that landed on
 * one would take the keyboard somewhere invisible.
 */
function paneRects(dock: DockviewApi): PaneRect[] {
  const rects: PaneRect[] = [];
  for (const group of dock.groups) {
    const box = group.element.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    rects.push({ id: group.id, left: box.left, top: box.top, width: box.width, height: box.height });
  }
  return rects;
}

const groupById = (dock: DockviewApi, id: string): DockviewGroupPanel | undefined =>
  dock.groups.find((group) => group.id === id);

/**
 * The pane the keyboard is in.
 *
 * dockview's `activeGroup` is the answer, but it can be momentarily unset — a
 * window that has never been clicked, or one whose active group was just
 * removed. Falling back to the active panel's group and then to the first pane
 * matters: a split chord that answers "nothing is focused" because an overlay
 * closed a moment ago is a chord the user learns not to trust.
 */
function focusedGroup(dock: DockviewApi): DockviewGroupPanel | undefined {
  return dock.activeGroup ?? dock.activePanel?.api.group ?? dock.groups[0];
}

/** Bring a pane forward *and* put the keyboard in it — `setActive` on the
 * group's visible panel is dockview's own path for both, and it is what makes
 * `Alt+→` followed by typing land where the user is looking. */
function focusGroup(group: DockviewGroupPanel | undefined): void {
  const panel = group?.activePanel;
  if (panel === undefined) return;
  panel.api.setActive();
  panel.focus();
}

const toast = (kind: "warn" | "error", message: string): void =>
  useStore.getState().pushToast(kind, message);

// ---- moving between panes ---------------------------------------------------

export function focusPaneInDirection(direction: PaneDirection): void {
  const dock = dockApiHandle();
  const from = dock === null ? undefined : focusedGroup(dock);
  if (dock === null || from === undefined) return;
  const target = paneInDirection(paneRects(dock), from.id, direction);
  if (target !== null) focusGroup(groupById(dock, target));
}

export function cycleFocusedPane(step: number): void {
  const dock = dockApiHandle();
  const from = dock === null ? undefined : focusedGroup(dock);
  if (dock === null || from === undefined) return;
  const target = cyclePane(paneRects(dock), from.id, step);
  if (target !== null) focusGroup(groupById(dock, target));
}

/**
 * Trade two panes' contents, keeping both boxes where they are.
 *
 * dockview destroys a group the moment its last panel leaves, so the obvious
 * "move A's panels to B, then B's to A" cannot work when each holds one panel —
 * A is already gone by the second step. Instead: move this pane's panels *into*
 * the neighbour (which takes over the union of the two boxes), then push the
 * neighbour's original panels back out on the far side. The sizes are put back
 * afterwards, because the grid would otherwise split the union evenly and a
 * swap that silently resizes both panes is not a swap.
 */
export function swapPaneInDirection(direction: PaneDirection): void {
  const dock = dockApiHandle();
  const from = dock === null ? undefined : focusedGroup(dock);
  if (dock === null || from === undefined) return;
  const targetId = paneInDirection(paneRects(dock), from.id, direction);
  const target = targetId === null ? undefined : groupById(dock, targetId);
  if (target === undefined) return;

  const mine: IDockviewPanel[] = [...from.panels];
  const theirs: IDockviewPanel[] = [...target.panels];
  if (mine.length === 0 || theirs.length === 0) return;
  const myBox = { width: from.api.width, height: from.api.height };
  const theirBox = { width: target.api.width, height: target.api.height };

  for (const panel of mine) panel.api.moveTo({ group: target, position: "center" });
  const back = oppositeDirection(direction);
  const first = theirs[0];
  if (first === undefined) return;
  first.api.moveTo({ group: target, position: MOVE_POSITION[back] });
  const landed = first.api.group;
  for (const panel of theirs.slice(1)) panel.api.moveTo({ group: landed, position: "center" });

  landed.api.setSize(myBox);
  target.api.setSize(theirBox);
  focusGroup(target);
}

/**
 * Close the focused pane's visible panel — never the last one in the window.
 *
 * A dock with nothing in it is not a state the app has a way out of (there is
 * no tab to click and no group to split), so the floor is one pane. "Switch to
 * the Default layout" is how you get the rest back.
 */
export function closeFocusedPane(): void {
  const dock = dockApiHandle();
  if (dock === null) return;
  if (dock.panels.length <= 1) {
    toast("warn", "That is the last pane — Workbench needs one.");
    return;
  }
  // The focused pane's visible panel, by the same rule everything else here
  // uses — a group holding three editor tabs is one pane with three things in
  // it, and "close this pane" must not throw away two of them unasked.
  focusedGroup(dock)?.activePanel?.api.close();
}

// ---- splitting --------------------------------------------------------------

/** Place `choice` in a new pane beside `reference`. Exported for the tests and
 * for the tab-strip buttons; the commands go through `splitFocusedPane`. */
export async function placeChoice(
  choice: PaneChoice,
  referenceId: string,
  direction: SplitDirection,
): Promise<void> {
  const dock = dockApiHandle();
  if (dock === null) return;
  // Awaited *before* the group is looked up: "New agent session" is a round
  // trip to the server, and the arrangement may have moved on meanwhile.
  const key = await choice.key();
  // A row that is not the tool's default pane and answered with no key did not
  // get what it was going to bind to — a session that failed to start. Opening
  // the tool's default pane instead would silently do something else.
  if (key === null && choice.defaultPane !== true) return;
  const reference = groupById(dock, referenceId) ?? focusedGroup(dock);
  if (reference === undefined) return;
  const id = paneId(choice.toolId, key);

  const existing = dock.getPanel(id);
  if (existing !== undefined) {
    // One pane per identity: the pane showing that session/file/terminal moves
    // into the split rather than being cloned, which is both what a tiling
    // window manager does and what keeps a pane id meaningful.
    if (existing.api.group === reference && reference.panels.length === 1) {
      focusGroup(reference);
      return;
    }
    existing.api.moveTo({ group: reference, position: MOVE_POSITION[direction] });
    existing.api.setActive();
    existing.focus();
    markNewPane(existing);
    return;
  }
  const panel = dock.addPanel({
    id,
    component: choice.toolId,
    title: paneTitle(TOOLS, id),
    position: { referenceGroup: reference, direction: ADD_DIRECTION[direction] },
  });
  panel.api.setActive();
  panel.focus();
  markNewPane(panel);
}

/**
 * The seam the motion lane lands on.
 *
 * A pane arriving is the one panel-level change worth confirming with movement
 * (DESIGN.md §5: opacity/transform only). The class is added here and removed
 * by the animation itself; `panes.css` owns the timing and reads it from the
 * tokens — it is on the **travel** channel (`--motion-enter`) now that the
 * motion vocabulary has landed, which is the seam this was left as. Reduced
 * motion zeroes the travel token and keeps the fade, so there is nothing to
 * gate here.
 */
function markNewPane(panel: IDockviewPanel): void {
  const element = panel.api.group.element;
  element.classList.remove("wb-pane-new");
  // Next frame: re-adding a class in the same frame it was removed does not
  // restart a CSS animation.
  requestAnimationFrame(() => {
    element.classList.add("wb-pane-new");
    element.addEventListener("animationend", () => element.classList.remove("wb-pane-new"), {
      once: true,
    });
  });
}

/**
 * Put `toolId#key` on screen — the "open this thing" gesture, as opposed to the
 * "split here and choose" gesture `placeChoice` serves. A null key is the
 * tool's own default pane.
 *
 * The difference is what happens when the pane already exists. A split *moves*
 * it next to the pane you split, because that is what you asked for; opening
 * something you already have open must not rearrange the window, so this
 * focuses it where it is. That is the whole of "opening the same conversation
 * twice focuses the existing pane rather than cloning it" — the rule falls out
 * of pane identity (`ui/src/panes.ts`) rather than being enforced by a caller.
 *
 * This is also what a **plural** tool's "open my panel" command must use.
 * `openToolPanel` (registry.ts) deliberately mints a second pane when a plural
 * tool's panel is already open — that is right for a tool you want N of, and
 * wrong for the command that means "show me the browser", which would otherwise
 * hand you a duplicate every time you ran it (and persist its throwaway key).
 *
 * Lives here rather than in the tool that calls it: placing panes is the pane
 * capability's job, and a second copy of `addPanel` elsewhere is how the two
 * would drift.
 */
export function revealPane(
  toolId: string,
  key: string | null,
  direction: SplitDirection = "right",
): void {
  const dock = dockApiHandle();
  if (dock === null) return;
  const id = paneId(toolId, key);
  const existing = dock.getPanel(id);
  if (existing !== undefined) {
    existing.api.setActive();
    existing.focus();
    return;
  }
  const reference = focusedGroup(dock);
  const panel = dock.addPanel({
    id,
    component: toolId,
    title: paneTitle(TOOLS, id),
    ...(reference === undefined
      ? {}
      : { position: { referenceGroup: reference, direction: ADD_DIRECTION[direction] } }),
  });
  panel.api.setActive();
  panel.focus();
  markNewPane(panel);
}

/** Rows for the picker: everything the registry can put in a pane. */
function pickerRows(referenceId: string, direction: SplitDirection): QuickPickRow[] {
  return paneInstanceOptions(TOOLS).map((choice) => ({
    key: `${choice.toolId}:${choice.id}`,
    title: choice.title,
    ...(choice.detail !== undefined ? { detail: choice.detail } : {}),
    category: choice.category,
    ...(choice.disabled === true ? { disabled: true } : {}),
    run: () => void placeChoice(choice, referenceId, direction),
  }));
}

/** Split this pane and ask what goes in the new one. */
export function splitGroup(group: DockviewGroupPanel, direction: SplitDirection): void {
  useStore.getState().openQuickPick({
    label: direction === "right" ? "Split this pane to the right" : "Split this pane downwards",
    placeholder: "What goes in the new pane?",
    rows: () => pickerRows(group.id, direction),
  });
}

export function splitFocusedPane(direction: SplitDirection): void {
  const dock = dockApiHandle();
  const group = dock === null ? undefined : focusedGroup(dock);
  if (group === undefined) {
    toast("warn", "No pane is focused — click one first.");
    return;
  }
  splitGroup(group, direction);
}

// ---- popping a pane out to its own window -----------------------------------

/**
 * Where dockview opens a popped-out pane. It is a real same-origin page
 * (`ui/public/popout.html`, copied verbatim to the build — no module script, so
 * it is not a Rollup entry) because dockview navigates a browser window to it and
 * then re-parents the pane's DOM in — Monaco and xterm are *moved*,
 * not recreated, so they survive the trip (asserted in `e2e/popout.spec.ts`).
 */
const POPOUT_URL = "/popout.html";

/**
 * Carry the app's theme into the popped-out document.
 *
 * dockview copies the main window's stylesheets into the popout on its `load`
 * event, so the design tokens arrive — but the `data-theme` attribute that
 * selects the light palette is on the main document's `<html>`, and that is not
 * copied. `popout.html` sets it from `localStorage` before first paint; this
 * mirrors the *live* theme (which may be a flip the user has not persisted yet),
 * once now and again on load in case the document is still the blank pre-nav one.
 */
function themePopoutWindow(win: Window): void {
  const apply = (): void => {
    try {
      const el = win.document.documentElement;
      if (documentTheme() === "light") el.setAttribute("data-theme", "light");
      else el.removeAttribute("data-theme");
    } catch {
      // The window was closed, or is briefly cross-origin mid-navigation.
    }
  };
  apply();
  win.addEventListener("load", apply);
}

// ---- the popout lifecycle ---------------------------------------------------

/**
 * Everything that has to happen to a popped-out window, wherever it came from.
 *
 * dockview 7 announces popout windows as they open (`onDidAddPopoutGroup`) and
 * enumerates the open ones (`getPopouts()`). Before that, the only hook was the
 * `onDidOpen` option on the `addPopoutGroup` call *this file* makes — one
 * callback, for the one window that call opened, once. So a popout dockview
 * opened by itself, restoring one from `layouts.json`, went un-themed: it got
 * whatever `popout.html` read out of `localStorage`, which is the last
 * *persisted* theme rather than the one on screen. And no popout could ever be
 * re-themed afterwards, because nothing in the app still held a handle to it.
 *
 * Both are fixed here: every popout is themed as it appears, whoever opened it,
 * and a theme flip repaints the ones already out.
 *
 * `onDidRemovePopoutGroup` is deliberately not subscribed to — a closing window
 * takes its own listeners with it and the pane it held re-grids itself, so
 * there is nothing this tool owns to undo.
 */
let popoutListeners: DockviewIDisposable[] = [];
let unsubscribeTheme: (() => void) | null = null;

function onDockReady(dock: DockviewApi | null): void {
  for (const listener of popoutListeners) listener.dispose();
  popoutListeners = [];
  unsubscribeTheme?.();
  unsubscribeTheme = null;
  if (dock === null) return;
  popoutListeners = [dock.onDidAddPopoutGroup(({ window }) => themePopoutWindow(window))];
  // Repaint the windows that are already out. The store is the app-wide theme
  // (`store.ts`), and `documentTheme()` inside `themePopoutWindow` reads the
  // attribute the flip has already set, so this needs no argument.
  unsubscribeTheme = useStore.subscribe((state, previous) => {
    if (state.theme === previous.theme) return;
    for (const popout of dock.getPopouts()) themePopoutWindow(popout.window);
  });
}

/**
 * Pop the focused pane out into a separate window, unless a pane in it refuses.
 *
 * The refusal is the gating safety decision (M5 item 13): a native Office window
 * docked in a pane has no HWND in a popped-out window's parent chain, so it
 * cannot follow and must not be orphaned. Every panel in the target group is put
 * to the registry's `popoutGuard`s — not the office store directly, because the
 * office host is a document view inside an `editors#…` pane, not a pane of its
 * own — and one blocking reason refuses the whole group.
 *
 * `addPopoutGroup` resolves `false` when the browser blocked the popup; dockview
 * has already put the group back by then, so the only thing left is to say so.
 */
export async function popOutFocusedPane(): Promise<void> {
  const dock = dockApiHandle();
  const group = dock === null ? undefined : focusedGroup(dock);
  if (dock === null || group === undefined) {
    toast("warn", "No pane is focused — click one first.");
    return;
  }
  const reason = popoutRefusal(
    TOOLS,
    group.panels.map((panel) => panel.id),
  );
  if (reason !== null) {
    toast("warn", reason);
    return;
  }
  // dockview fires `onDidOpenPopoutWindowFail` *synchronously* inside
  // `addPopoutGroup` when the browser blocks the popup. That event is the layout
  // system's only signal for the restore case, so `Layouts.tsx` toasts on it —
  // but it fires on this interactive path too, and this path already reports the
  // block below with wording aimed at the click the user just made. The guard
  // tells the restore handler to stay quiet so only one, correctly-worded toast
  // shows (`popoutInProgress`, read in `Layouts.tsx`'s `onDidOpenPopoutWindowFail`).
  popoutInProgress = true;
  let opened: boolean;
  try {
    // No `onDidOpen` here any more: theming is `onDidAddPopoutGroup`'s job now,
    // so that a window dockview opens on its own — restoring one from
    // `layouts.json` — is themed by the same code as one this call opens.
    opened = await dock.addPopoutGroup(group, { popoutUrl: POPOUT_URL });
  } finally {
    popoutInProgress = false;
  }
  if (!opened) {
    toast("warn", "The browser blocked the pop-out window — allow pop-ups for Workbench and try again.");
  }
}

/**
 * True only while an interactive `popOutFocusedPane` is awaiting `addPopoutGroup`.
 *
 * `Layouts.tsx`'s `onDidOpenPopoutWindowFail` handler reads it to tell an
 * interactive pop-out block (which reports itself) from a restore-time one (which
 * has no other signal) and so avoid stacking two toasts on the same failure.
 */
let popoutInProgress = false;
export const isPoppingOut = (): boolean => popoutInProgress;

/**
 * Return a popped-out pane to the grid — the keyboard path for what closing the
 * popout window already does (dockview re-grids it at its `gridReferenceGroup`).
 *
 * Prefers the focused pane when it is the popped-out one, so with several out at
 * once the command acts on the one the user is pointed at; otherwise it takes
 * any. Closing the window is what triggers dockview's own re-grid — there is no
 * public "move this popout back" call, and closing is the same path a restart or
 * a blocked popup takes.
 *
 * "Any" is `getPopouts()` (dockview 7) rather than a scan of `dock.groups` for a
 * `location.type === "popout"`: with nested popout windows a group in the grid
 * is no longer the only thing that can be out there, and the enumeration is the
 * library's own answer to the question.
 */
export function popInFocusedPane(): void {
  const dock = dockApiHandle();
  if (dock === null) return;
  const focused = focusedGroup(dock);
  if (focused !== undefined && focused.api.location.type === "popout") {
    focused.api.location.getWindow().close();
    return;
  }
  const popout = dock.getPopouts()[0];
  if (popout === undefined) {
    toast("warn", "No pane is popped out.");
    return;
  }
  popout.window.close();
}

// ---- the split affordance ---------------------------------------------------

function SplitRightIcon() {
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
      <path d="M8 2.6v10.8" />
    </svg>
  );
}

function SplitDownIcon() {
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
      <path d="M1.6 8h12.8" />
    </svg>
  );
}

/**
 * Two glyphs at the right end of the **focused** pane's tab strip.
 *
 * Only the focused one: chrome recedes (DESIGN.md §1.1), and a control on every
 * tab strip in a six-pane window is five controls nobody is looking at. Where
 * the keyboard is, is exactly where the mouse path belongs.
 *
 * Each tooltip names its chord, and asks the registry for it rather than
 * spelling it out (DESIGN.md §6.13): a hardcoded keycap in a tooltip is stale
 * the day the chord below moves, and a mouse path that teaches the wrong
 * keyboard path is worse than one that teaches none.
 */
function PaneActions(props: IDockviewHeaderActionsProps) {
  if (!props.isGroupActive) return null;
  return (
    <div className="wb-pane-actions">
      <button
        type="button"
        className="wb-pane-action"
        aria-label="Split this pane to the right"
        title={chordTooltip("Split right", "pane.split.right")}
        onClick={() => splitGroup(props.group, "right")}
      >
        <SplitRightIcon />
      </button>
      <button
        type="button"
        className="wb-pane-action"
        aria-label="Split this pane downwards"
        title={chordTooltip("Split down", "pane.split.down")}
        onClick={() => splitGroup(props.group, "down")}
      >
        <SplitDownIcon />
      </button>
    </div>
  );
}

// ---- registration -----------------------------------------------------------

const DIRECTION_LABEL: Record<PaneDirection, string> = {
  left: "left",
  right: "right",
  up: "above",
  down: "below",
};

const ARROW: Record<PaneDirection, string> = {
  left: "ArrowLeft",
  right: "ArrowRight",
  up: "ArrowUp",
  down: "ArrowDown",
};

const focusCommands: ToolCommand[] = PANE_DIRECTIONS.map((direction) => ({
  id: `pane.focus.${direction}`,
  title: `Focus the pane ${DIRECTION_LABEL[direction]}`,
  category: PANES_CATEGORY,
  run: () => focusPaneInDirection(direction),
}));

const swapCommands: ToolCommand[] = PANE_DIRECTIONS.map((direction) => ({
  id: `pane.swap.${direction}`,
  title: `Swap this pane with the one ${DIRECTION_LABEL[direction]}`,
  category: PANES_CATEGORY,
  run: () => swapPaneInDirection(direction),
}));

/**
 * The chords, and why this tool takes as many as it does.
 *
 * Every `Alt` chord a tool registers is one a user's `shortcuts.md` can no
 * longer bind (`docs/tools.md`), so the bar is "a reflex, not a lookup". Moving
 * between panes *is* the reflex this milestone exists to create, and the arrow
 * set is the vocabulary every tiling window manager already taught: `Alt` moves
 * you, `Alt+Shift` moves the pane. The three letters are the tmux ones —
 * **s**plit, cycle (`o`), close (`x`) — and `Alt` is the only modifier that
 * reaches Workbench from inside xterm and Monaco, which is where you are when
 * you want to leave. Cycling backwards is deliberately chordless: it is
 * reachable from the QuickBar and does not deserve a fourth letter.
 */
const chords: Record<string, string[]> = {
  "pane.split.right": ["Alt+S"],
  "pane.split.down": ["Alt+Shift+S"],
  "pane.cycle": ["Alt+O"],
  "pane.close": ["Alt+X"],
  // `Alt+P` — **p**op out, the one letter left in the tmux set. Pop-in is
  // chordless like cycle-back: it is the rarer half and reachable from the
  // QuickBar, so it does not spend an `Alt` a `shortcuts.md` file could bind.
  "pane.popout": ["Alt+P"],
  ...Object.fromEntries(
    PANE_DIRECTIONS.map((direction) => [`pane.focus.${direction}`, [`Alt+${ARROW[direction]}`]]),
  ),
  ...Object.fromEntries(
    PANE_DIRECTIONS.map((direction) => [
      `pane.swap.${direction}`,
      [`Alt+Shift+${ARROW[direction]}`],
    ]),
  ),
};

export const panesTool: WorkbenchTool = {
  id: "panes",
  title: "Panes",
  commands: [
    {
      id: "pane.split.right",
      title: "Split this pane to the right…",
      detail: () => "then pick what goes in it",
      category: PANES_CATEGORY,
      run: () => splitFocusedPane("right"),
    },
    {
      id: "pane.split.down",
      title: "Split this pane downwards…",
      detail: () => "then pick what goes in it",
      category: PANES_CATEGORY,
      run: () => splitFocusedPane("down"),
    },
    ...focusCommands,
    ...swapCommands,
    {
      id: "pane.cycle",
      title: "Focus the next pane",
      category: PANES_CATEGORY,
      run: () => cycleFocusedPane(1),
    },
    {
      id: "pane.cycleBack",
      title: "Focus the previous pane",
      category: PANES_CATEGORY,
      run: () => cycleFocusedPane(-1),
    },
    {
      id: "pane.close",
      title: "Close this pane",
      category: PANES_CATEGORY,
      run: closeFocusedPane,
    },
    {
      id: "pane.popout",
      title: "Pop this pane out to its own window",
      detail: () => "moves it to a separate window; a docked Office window refuses",
      category: PANES_CATEGORY,
      run: () => void popOutFocusedPane(),
    },
    {
      id: "pane.popin",
      title: "Bring a popped-out pane back in",
      category: PANES_CATEGORY,
      run: popInFocusedPane,
    },
  ],
  shortcuts: chords,
  groupActions: PaneActions,
  // The pane capability operates on the dock rather than living in it, so it
  // takes the handle the same way the layout system does — here, to follow
  // popout windows through their lifecycle (`onDockReady` above).
  onDockReady,
};
