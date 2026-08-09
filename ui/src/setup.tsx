/**
 * First-run / Setup — the *connect* half of first run (M7 §2).
 *
 * The welcome card (M5 item 17, `panels/Keyboard.tsx`) teaches **the window** —
 * panes, the QuickBar, the tools. Setup teaches **the connections**: is Claude
 * signed in, can a real Office document be docked, is the OnlyOffice fallback
 * there. It composes with the welcome card rather than replacing it — same
 * discovery doctrine (DESIGN.md §6.13): it interrupts nothing, it is a panel and
 * not a modal, its affordances are real, and dismissal is workspace state.
 *
 * ## Why this file, and why it is light
 *
 * This is the tool's **descriptor** module — the one `tools.ts` imports, so it
 * is on the eager launch path. It therefore holds only what the shell needs
 * before the panel is ever opened: the dismissal state, the status-bar chip, the
 * commands, and the auto-open lifecycle. The panel *body* — the checklist UI and
 * its stylesheet — lives in `panels/Setup.tsx` and is reached through a **dynamic
 * `import()`** (`React.lazy` + a warm on idle), so none of it enters the entry
 * chunk (`ui/e2e/perf/bundle.spec.ts`, ARCHITECTURE.md "the launch path").
 *
 * ## Where the dismissal lives
 *
 * `<workspace>/.workbench/setup.json`, written through the ordinary files API —
 * the neighbour of `welcome.json` and `layouts.json`, and for the same reasons
 * the welcome card chose a file over `localStorage`: the shell and a browser tab
 * agree, and a new project is a new window. Unreadable or malformed content
 * counts as dismissed — the failure direction that never nags.
 */

import type { DockviewApi, IDockviewPanelProps } from "dockview";
import { lazy, Suspense } from "react";
import { create } from "zustand";

import * as api from "./api";
import { closePanel, openPanel } from "./dock";
import { forgetFirstRun, greetFirstRun, type FirstRunSurface } from "./firstRun";
import type { ToolCommand, WorkbenchTool } from "./registry";
import type { SetupStatus } from "./types";

/** Stable contract: a saved layout references this panel by it (`docs/tools.md`). */
const TOOL_ID = "setup";

const SETUP_FILE = ".workbench/setup.json";

// ---- state (light: nothing here pulls in the panel body) --------------------

interface SetupState {
  /** `null` until the workspace has answered — so a dismissed window never
   * flashes the walkthrough (the welcome card's rule). */
  dismissed: boolean | null;
  /** The server's honest read, fetched once on dock-ready and refreshed when the
   * panel opens. `null` before the first answer; the chip stays hidden until then. */
  status: SetupStatus | null;
}

/**
 * This tool's own store. zustand stays the only state library and `store.ts` the
 * home for app-wide state; nothing outside this module reads whether Setup has
 * been dismissed or what it found, so it lives here (CLAUDE.md, `docs/tools.md`).
 */
const useSetup = create<SetupState>()(() => ({ dismissed: null, status: null }));

export { useSetup };

/** Present-but-unreadable still means "this window has been here before". Only a
 * genuine 404 is first run; every other failure counts as dismissed, so one bad
 * GET during launch never reopens the walkthrough on a used workspace (the exact
 * rule `panels/Keyboard.tsx` documents for the welcome card). */
function parseDismissed(content: string): boolean {
  try {
    const parsed: unknown = JSON.parse(content);
    return (parsed as { dismissed?: unknown }).dismissed !== false;
  } catch {
    return true;
  }
}

async function readDismissed(): Promise<boolean> {
  try {
    return parseDismissed((await api.getFileContent(SETUP_FILE)).content);
  } catch (error) {
    return !(error instanceof api.ApiError && error.status === 404);
  }
}

async function writeDismissed(dismissed: boolean): Promise<void> {
  try {
    await api.putFileContent({
      path: SETUP_FILE,
      content: `${JSON.stringify({ dismissed }, null, 2)}\n`,
    });
  } catch {
    // storage unavailable — the walkthrough just comes back next launch
  }
}

/** Read the honest status into the store. A failure leaves it `null`, which the
 * chip reads as "nothing to say" rather than an error banner. */
async function refreshStatus(): Promise<void> {
  try {
    useSetup.setState({ status: await api.getSetupStatus() });
  } catch {
    // backend not up yet, or gone — the chip stays hidden, the panel says so
  }
}

/** The walkthrough is over: never auto-opens again, and its tab retires. */
export function dismissSetup(): void {
  useSetup.setState({ dismissed: true });
  void writeDismissed(true);
  closePanel(TOOL_ID);
}

/** The panel body's chunk, resolved before the panel is opened. `React.lazy`
 * caches this import, so preloading it means the panel mounts **without
 * suspending** — no loading flash, and (the reason it matters for auto-open) the
 * `setActive` that brings the tab forward is not raced by the chunk landing a
 * beat later and re-rendering. Idempotent and cheap once warmed on idle. */
async function loadPanel(): Promise<void> {
  try {
    await import("./panels/Setup");
  } catch {
    // The panel opens on the Suspense fallback instead; not worth blocking on.
  }
}

/** Open the walkthrough by hand (the QuickBar command / the chip). Does **not**
 * clear the dismissal — opening it to look is not un-answering it, so it will
 * not start auto-opening again. */
function openSetup(): void {
  void refreshStatus();
  void loadPanel().then(() => openPanel(TOOL_ID));
}

/**
 * Setup's half of the first-run greeting.
 *
 * The "is this window already arranged?" question is *not* asked here any more:
 * it is shared with the welcome card and asked once by `firstRun.ts`, because
 * opening either panel is itself a layout change that would flip the answer for
 * the other one — which is how first run came to show one teaching tab, the
 * other, or both, depending on nothing the user did. Setup still owns its own
 * dismissal, which is true whether or not it ends up opening.
 */
const setupFirstRun: FirstRunSurface = {
  /**
   * Setup opens **last, and is the tab a stranger lands on**, with the welcome
   * card beside it. It is the actionable half — it is the surface that can say
   * "Claude is not signed in, run `claude /login`" — and it carries a *Show the
   * welcome card* button, so the window basics are one click away from inside
   * it. Nothing is hidden either way: both are centre tabs.
   */
  order: 20,
  wanted: async () => {
    const dismissed = await readDismissed();
    useSetup.setState({ dismissed });
    if (dismissed) return false;
    // Load the body before the greeting opens anything, so the pane mounts
    // front-and-active rather than suspending while another tab keeps the
    // focus (see `loadPanel`). Done here rather than in `open` so the chunk is
    // fetched while the shared arrangement question is still in flight.
    await loadPanel();
    return true;
  },
  open: () => openPanel(TOOL_ID),
};

/** Warm the panel chunk when the browser is idle, so a returning user's "Show
 * setup…" opens instantly rather than paying the dynamic import on the click.
 * No `timeout`: a page that never idles simply loads it on demand. */
function warmPanel(): void {
  const warm = (): void => void loadPanel();
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback;
  if (idle === undefined) setTimeout(warm, 1_000);
  else idle(warm);
}

function onDockReady(dock: DockviewApi | null): void {
  if (dock === null) {
    useSetup.setState({ dismissed: null, status: null });
    forgetFirstRun();
    return;
  }
  void refreshStatus();
  greetFirstRun(dock, setupFirstRun);
  warmPanel();
}

// ---- the panel body, dynamically imported -----------------------------------

const SetupLazy = lazy(() => import("./panels/Setup"));

/** The dockview panel component: a Suspense boundary around the lazily-loaded
 * body, so none of the checklist UI or its stylesheet is on the launch path. */
function SetupPanel(props: IDockviewPanelProps) {
  return (
    <Suspense fallback={<div className="wb-setup-loading u-label">Loading…</div>}>
      <SetupLazy {...props} />
    </Suspense>
  );
}

// ---- the status chip (eager: reuses existing status-bar classes) ------------

function SetupIcon() {
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
      <path d="M8 1.6v2.2M8 12.2v2.2M14.4 8h-2.2M3.8 8H1.6M12.5 3.5l-1.6 1.6M5.1 10.9l-1.6 1.6M12.5 12.5l-1.6-1.6M5.1 5.1 3.5 3.5" />
      <circle cx="8" cy="8" r="2.4" />
    </svg>
  );
}

/**
 * The chip's reading, as a pure function — the quiet-bar rule (DESIGN.md §6.7)
 * made unit-testable without a renderer. `null` hides the chip; a string is its
 * label. It hides the moment `all_ok`, and also once the walkthrough is
 * **dismissed** — getting out of the way for good is the whole promise (M7 §2:
 * "never nags again once dismissed"). So it is the live reading of an
 * *unanswered* first run; a user who dismissed it reaches Setup again from the
 * QuickBar's "Show setup" row, like every other command.
 */
export function setupChipLabel(dismissed: boolean | null, status: SetupStatus | null): string | null {
  if (dismissed !== false || status === null || status.all_ok) return null;
  const pending = status.checks.filter((c) => c.state === "action_needed").length;
  return pending > 0 ? `Setup (${String(pending)})` : "Setup";
}

/** Status bar, right region. On a signed-in machine the bar carries nothing for
 * Setup at all (see {@link setupChipLabel}). */
function SetupStatusChip() {
  const dismissed = useSetup((s) => s.dismissed);
  const status = useSetup((s) => s.status);
  const label = setupChipLabel(dismissed, status);
  if (label === null) return null;
  return (
    <button
      type="button"
      className="wb-status-chip wb-setup-chip"
      title="What is connected — Claude sign-in, Office"
      onClick={openSetup}
    >
      <SetupIcon />
      <span className="wb-status-chip-title">{label}</span>
    </button>
  );
}

// ---- registration -----------------------------------------------------------

const commands: readonly ToolCommand[] = [
  {
    id: "setup.open",
    title: "Show setup",
    detail: () => "what is connected: Claude sign-in, Office, OnlyOffice",
    run: openSetup,
  },
];

export const setupTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Setup",
  icon: SetupIcon,
  panel: {
    component: SetupPanel,
    // Centre, the welcome card's own position: it opens as a tab in the pane the
    // window is built around, so it interrupts nothing — another tab is one
    // click away and the panel closes for good.
    defaultLocation: { area: "center" },
    openByDefault: false,
    // One walkthrough is enough; two would be two of the same checklist.
    singleton: true,
  },
  commands,
  // No `Alt` chord, deliberately — the Scratchpad precedent (`docs/tools.md`): a
  // registered chord is taken from the user's `shortcuts.md` silently, and a
  // once-per-workspace walkthrough does not earn that price. It is reachable from
  // the QuickBar row and, until it is answered, the status chip.
  statusContributions: [{ region: "right", component: SetupStatusChip }],
  // No `onWorkspaceChanged`, by the welcome card's precedent: the walkthrough is
  // a launch-time greeting, not something a mid-session workspace switch
  // re-triggers. A switched-into project's first-run greeting waits for its next
  // launch, exactly as the welcome card does.
  onDockReady,
};
