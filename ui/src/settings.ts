/**
 * Settings — the in-app surface for the knobs that were environment variables
 * (M7 V8): the theme, native Office hosting, voice input, and the telemetry
 * stance, which is *off* and says so rather than offering a switch.
 *
 * ## Why this file, and why it holds no markup
 *
 * This is the tool's **descriptor** module — the one `tools.ts` imports, so it
 * is on the eager launch path. It therefore holds only what the shell needs
 * before the panel is ever opened: the store, the command, and the theme wiring
 * that has to run whether or not anyone opens Settings. The panel *body* — the
 * controls and their stylesheet — lives in `panels/Settings.tsx` behind a
 * **dynamic `import()`** (`React.lazy` + a warm on idle), so none of it enters
 * the entry chunk (`ui/e2e/perf/bundle.spec.ts`, ARCHITECTURE.md "the launch
 * path"). That is also why there is not a single tag in here: the three
 * elements it does need are built with `createElement`, which costs nothing and
 * keeps the file free of a JSX pipeline for a gear glyph and a fallback line.
 *
 * ## Where the settings live
 *
 * `<app data>/settings.json`, through `GET`/`PUT /api/settings` — machine-local
 * state, never the workspace and **never `~/.claude`** (`services/settings.py`
 * and the security posture in `CLAUDE.md`). The server is the authority on what
 * is stored *and* on what is actually in force: an operator's
 * `WORKBENCH_OFFICE_NATIVE` outranks the stored choice, and the state says so,
 * which is what lets the panel disable that control with a reason instead of
 * offering a button that does nothing.
 *
 * ## Writes are ordered, because clicks are not
 *
 * Every change PUTs the whole document, and a panel of segmented controls is
 * clicked faster than a round trip. Two rules keep that honest, and both are
 * spelled out at "ordering" below: a reply is applied only if it is the newest
 * exchange issued, and a patch merges onto the document we *intend* to store
 * rather than onto the last one that settled. Writes are serialized and
 * coalesced on top of that, so the file and the panel cannot disagree about
 * which click won.
 *
 * ## The theme is the one setting with two homes, on purpose
 *
 * `localStorage` stays the **pre-paint cache** — `index.html` reads it before
 * the first frame, and a round trip cannot happen that early without flashing
 * the wrong palette. The server document is the **authority**: on load the
 * stored choice is applied through the app store's own `setTheme` (which
 * refreshes the cache), and a theme changed anywhere else in the app — the
 * QuickBar's *Toggle theme* — is written back here. That subscription is why
 * the toggle survives a relaunch, and it is deliberately in this module rather
 * than in `store.ts`: the store names no capability, and persisting a
 * preference is this capability's job.
 *
 * One consequence, stated rather than discovered: on the *first* launch after
 * this lands, a window whose cached theme disagrees with the (still default)
 * stored choice repaints once, to what the stored choice resolves to — the OS
 * preference, which is what an app with no answer yet should wear. From then on
 * the two agree, because every change goes through here.
 */

import type { DockviewApi, IDockviewPanelProps } from "dockview";
import { createElement, lazy, Suspense, type ReactElement } from "react";
import { create } from "zustand";

import * as api from "./api";
import { openPanel } from "./dock";
import type { ToolCommand, WorkbenchTool } from "./registry";
import { useStore } from "./store";
import { documentTheme, type Theme } from "./theme";
import type { SettingsState, ThemeChoice, WorkbenchSettings } from "./types";

/** Stable contract: a saved layout references this panel by it (`docs/tools.md`). */
const TOOL_ID = "settings";

// ---- state (light: nothing here pulls in the panel body) --------------------

interface SettingsUi {
  /** The server's answer — stored choices, what is in force, and why. `null`
   * until the first read, which the panel renders as "loading" rather than as
   * an empty form claiming everything is at its default. */
  state: SettingsState | null;
  /**
   * The last read or write that failed, as one sentence. Cleared by the next
   * attempt.
   *
   * There is deliberately no `saving` flag beside it: a write to a local file
   * settles in a few milliseconds, and a spinner that appears and disappears
   * inside one frame is noise the eye reads as a glitch. What a user needs to
   * know is when it did *not* work, which is this.
   */
  error: string | null;
}

/**
 * This tool's own store. zustand stays the only state library and `store.ts`
 * the home for app-wide state; nothing outside this module reads the settings
 * document, so it lives here (CLAUDE.md, `docs/tools.md`). The one value that
 * *is* app-wide — the theme currently on screen — stays in `store.ts` where it
 * always was, and this module keeps it in step with what is stored.
 */
const useSettings = create<SettingsUi>()(() => ({ state: null, error: null }));

export { useSettings };

// ---- theme: resolving, applying, and writing back ---------------------------

/** The OS preference, for `theme: "system"`. Falls back to dark — the app's own
 * default, and the value `index.html` paints when it cannot ask either. */
export function systemTheme(): Theme {
  const query = globalThis.matchMedia?.("(prefers-color-scheme: light)");
  return query?.matches === true ? "light" : "dark";
}

/** A stored choice as the palette to actually wear. */
export function resolveTheme(choice: ThemeChoice): Theme {
  return choice === "system" ? systemTheme() : choice;
}

/** Put the window in the theme the settings ask for. A no-op when it is already
 * wearing it, which is the common case — and the reason applying the stored
 * value on load cannot loop with the write-back subscription below. */
function applyTheme(choice: ThemeChoice): void {
  const wanted = resolveTheme(choice);
  if (wanted !== documentTheme()) useStore.getState().setTheme(wanted);
}

/**
 * The app's theme changed somewhere that is not this panel (the QuickBar's
 * *Toggle theme*, a chord, a relayed command). Persist it, so the next launch
 * opens in the theme the user left — the half that used to live only in
 * `localStorage` and therefore only in one browser profile.
 *
 * Nothing is written while the settings have not loaded (there is no document
 * to merge into yet) or when the stored choice already resolves to this theme,
 * which is what makes {@link applyTheme}'s own `setTheme` inert here.
 */
function onAppThemeChanged(theme: Theme): void {
  const base = intended();
  if (base === null || resolveTheme(base.theme) === theme) return;
  void save({ theme });
}

// ---- ordering: one writer, and only the newest answer lands -----------------
//
// A settings panel is clicked fast — theme, then voice, inside one round trip —
// and a whole-document PUT per click gives two ways to lose an answer. Both are
// closed here, and both are reproduced in `settings.test.ts`.
//
//  1. **A stale answer must not land.** Every exchange with the server takes a
//     ticket from `issued`, and its result is applied only if that ticket is
//     still the newest one. That covers a slow PUT replying after a later one,
//     and equally a GET that was already in flight when a write was issued —
//     `onDockReady` starts a read and then subscribes to the theme, so that
//     second one is a live path, not a hypothetical.
//  2. **A patch must merge onto what the user last chose**, not onto the last
//     answer that happened to settle. `desired` is the document we intend to
//     store; every patch merges onto *that*, so the body actually written is
//     cumulative instead of un-doing the click before it.
//
// Writes are also serialized — one PUT outstanding, the rest coalesced into the
// next body — which is what makes the *file* agree with the panel: two PUTs in
// flight at once can reach a single-threaded disk in either order, and no
// amount of client-side bookkeeping can decide that after the fact.

/** Monotonic ticket for every exchange that may write to the store. */
let issued = 0;

/** The document we intend to have stored, while a write is outstanding. */
let desired: WorkbenchSettings | null = null;

/** The write currently draining, so `save` starts one drain and no more. */
let writing: Promise<void> | null = null;

/**
 * What we believe will be stored: the newest queued write if there is one, and
 * otherwise the server's last answer. Every merge, and every "is this already
 * the case?" question, asks this rather than `state.stored` — which is a
 * *settled* answer and therefore behind for as long as a write is in flight.
 */
function intended(): WorkbenchSettings | null {
  return desired ?? useSettings.getState().state?.stored ?? null;
}

/** Forget any outstanding exchange: whatever is in flight loses its ticket and
 * its answer is dropped. The drain unwinds itself once `desired` is empty. */
function forget(): void {
  issued += 1;
  desired = null;
}

// ---- reading and writing ----------------------------------------------------

/** Read the document into the store and put the window in its theme. A failure
 * leaves the last good state in place; the panel shows the error. */
export async function refresh(): Promise<void> {
  // A write already outstanding is a newer question than this one, and its own
  // reply is the same `SettingsState` a GET would return. Standing down saves
  // the round trip and, more to the point, means no read can ever land on top
  // of a write that has not settled.
  if (writing !== null) return writing;
  const ticket = ++issued;
  try {
    const state = await api.getSettings();
    if (ticket !== issued) return; // superseded while it flew
    useSettings.setState({ state, error: null });
    applyTheme(state.effective.theme);
  } catch (error) {
    if (ticket !== issued) return;
    useSettings.setState({ error: message(error) });
  }
}

/**
 * Change one or more settings: merge onto the document we intend to store, PUT
 * it whole, and take the server's answer as the new truth (it carries the
 * overrides and the pending-restart list a client cannot derive).
 *
 * Whole-document writes, like layouts: there is no partial-update path to get
 * wrong. The promise settles when the change has actually reached the server —
 * including when it was coalesced into a later body — so `await save(…)` means
 * what it looks like it means.
 */
export function save(patch: Partial<WorkbenchSettings>): Promise<void> {
  const base = intended();
  if (base === null) return Promise.resolve(); // nothing loaded: no document to merge into
  desired = { ...base, ...patch };
  useSettings.setState({ error: null });
  return arm();
}

/** Ensure exactly one drain is running, and answer with it. */
function arm(): Promise<void> {
  const started =
    writing ??
    drain().finally(() => {
      writing = null;
      // A patch queued between the drain's last check and this line would
      // otherwise sit in `desired` forever. Nothing schedules user code in that
      // window today; re-arming costs a comparison and removes the reasoning.
      if (desired !== null) void arm();
    });
  writing = started;
  return started;
}

/** Send the intended document, then whatever replaced it while that was flying,
 * until nothing is queued. One PUT outstanding at a time, by construction. */
async function drain(): Promise<void> {
  while (desired !== null) {
    const body = desired;
    const ticket = ++issued;
    try {
      const state = await api.putSettings(body);
      if (desired === body) desired = null; // nothing newer queued while it flew
      if (ticket !== issued) continue; // superseded: this answer is history
      useSettings.setState({ state, error: null });
      applyTheme(state.effective.theme);
    } catch (error) {
      desired = null; // do not spin on a body the server just refused
      if (ticket === issued) useSettings.setState({ error: message(error) });
      return;
    }
  }
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// ---- lifecycle --------------------------------------------------------------

/** Unsubscribes the theme write-back; held so a dock teardown can drop it. */
let unsubscribeTheme: (() => void) | null = null;

/** The panel body's chunk. `React.lazy` caches the import, so warming it means
 * the panel mounts without suspending. Idempotent and cheap once warm. */
async function loadPanel(): Promise<void> {
  try {
    await import("./panels/Settings");
  } catch {
    // The panel opens on the Suspense fallback instead; not worth blocking on.
  }
}

/** Open Settings (the QuickBar row, `Ctrl+,`). Re-reads first: an override can
 * appear between launches, and the panel must never show a stale answer to
 * "what is actually in force". */
function openSettings(): void {
  void refresh();
  void loadPanel().then(() => {
    openPanel(TOOL_ID);
  });
}

/** Warm the panel chunk when the browser is idle, so the first `Ctrl+,` opens
 * instantly rather than paying the dynamic import on the keystroke. */
function warmPanel(): void {
  const warm = (): void => void loadPanel();
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback;
  if (idle === undefined) setTimeout(warm, 1_000);
  else idle(warm);
}

function onDockReady(dock: DockviewApi | null): void {
  unsubscribeTheme?.();
  unsubscribeTheme = null;
  if (dock === null) {
    // The state is dropped, so nothing still in flight may write to it either.
    forget();
    useSettings.setState({ state: null, error: null });
    return;
  }
  void refresh();
  // zustand's plain `subscribe` hands over the whole state; the theme is the
  // one field this cares about, so it is compared rather than selected — no
  // middleware, no second way to subscribe.
  unsubscribeTheme = useStore.subscribe((next, previous) => {
    if (next.theme !== previous.theme) onAppThemeChanged(next.theme);
  });
  warmPanel();
}

// ---- the panel wrapper (no JSX: see the module note) ------------------------

const SettingsLazy = lazy(() => import("./panels/Settings"));

/** A Suspense boundary around the lazily-loaded body, so none of the controls
 * or their stylesheet is on the launch path. */
function SettingsPanel(props: IDockviewPanelProps): ReactElement {
  return createElement(
    Suspense,
    {
      fallback: createElement("div", { className: "wb-settings-loading u-label" }, "Loading…"),
    },
    createElement(SettingsLazy, props),
  );
}

/** The tab glyph: sliders, not the gear the Setup walkthrough wears — the two
 * are neighbours in the QuickBar and must not read as the same capability. */
function SettingsIcon(): ReactElement {
  return createElement(
    "svg",
    {
      width: 14,
      height: 14,
      viewBox: "0 0 16 16",
      fill: "none",
      stroke: "currentColor",
      strokeWidth: 1.2,
      strokeLinecap: "round" as const,
      "aria-hidden": true,
    },
    createElement("path", { key: "rules", d: "M2 4.6h4.6M10.6 4.6H14M2 11.4h2.6M8.6 11.4H14" }),
    createElement("circle", { key: "top", cx: 8.6, cy: 4.6, r: 1.9 }),
    createElement("circle", { key: "bottom", cx: 6.6, cy: 11.4, r: 1.9 }),
  );
}

// ---- registration -----------------------------------------------------------

const commands: readonly ToolCommand[] = [
  {
    id: "settings.open",
    title: "Open Settings",
    detail: () => "theme, Office hosting, voice, privacy",
    run: openSettings,
  },
];

export const settingsTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Settings",
  icon: SettingsIcon,
  panel: {
    component: SettingsPanel,
    // Centre, as a tab: settings are a thing you go and look at, not a strip
    // that lives beside your work, and a modal would interrupt (DESIGN.md §6.13
    // — the discovery doctrine the welcome card and Setup already follow).
    defaultLocation: { area: "center" },
    openByDefault: false,
    // SINGULAR, and this is the reason the pane rules ask for. Plural is the
    // default for anything a user could want twice, but a Settings pane points
    // at no resource — there is one settings document per machine, and a second
    // pane would be a second view of the same three controls, each able to write
    // over the other's answer. Nothing here is keyed by an instance, so there is
    // nothing a second pane could be bound *to*. Opening it again focuses the
    // one that exists (`openPanel`).
    singleton: true,
  },
  commands,
  // `Ctrl+,` — the settings chord every editor a user has met already uses, and
  // it costs them nothing: `shortcuts.md` may bind only `Alt` chords, so this
  // takes no key out of their reach (unlike an `Alt` chord, which a registered
  // tool wins silently). Consequence worth knowing: a plain-`Ctrl` chord is not
  // intercepted while Monaco or a terminal has focus (`keys.ts`), so from inside
  // an editor it is the QuickBar row that opens Settings.
  shortcuts: { "settings.open": ["Ctrl+,"] },
  // No status contribution: the quiet-bar doctrine (DESIGN.md §6.7) is that a
  // chip earns its place by having something to report, and settings never do.
  // No `onWorkspaceChanged` either — the document is machine-local, so a switch
  // between projects changes nothing about it (`services/settings.py`).
  onDockReady,
};
