/**
 * Reconciliation specs — **ambient CI for workbooks** (productivity loops, PR-B).
 *
 * A checked-in `.workbench/reconcile/<name>.toml` maps workbook cells and ranges
 * to callables in the workspace's *own* code. Approve it once and a save of the
 * workbook re-runs the reconciliation gate with the values those callables
 * produce: nobody fires a check, and seconds later the chip flips.
 *
 * ## Why this file, and why it holds almost no markup
 *
 * This is the tool's **descriptor** module — the one `tools.ts` imports, so it
 * is on the eager launch path. It therefore holds only what the shell needs
 * before the panel is ever opened: the store, the command, and the status chip.
 * The panel *body* — the rows, the approval dialog copy and their stylesheet —
 * lives in `panels/SpecPanel.tsx` behind a **dynamic `import()`** (`React.lazy`
 * plus a warm on idle), so none of it enters the entry chunk (`e2e/perf/
 * bundle.spec.ts` asserts what is *inside* it, not only what it weighs). That is
 * also why there is no JSX here: the four elements it does need are built with
 * `createElement`, which costs nothing.
 *
 * ## The trust surface, in the client's words
 *
 * Nothing here can make a spec run. Approving is a REST call that echoes the
 * digest the row was **rendered with**, so a spec whose bytes moved between the
 * render and the click is refused (409) rather than approved on the strength of
 * what was on screen. The panel renders `approval.covered` verbatim, because the
 * sentence the copy uses — *this spec, running exactly this code, on this
 * machine, until either changes* — is only worth saying if the list behind it is
 * one a person can read.
 *
 * ## No singleton
 *
 * Specs live in a map keyed by `name`, never a `store.activeSpec`: a workspace
 * routinely proves several workbooks, and the panel lists all of them. State
 * lives in a second zustand instance **in this module**, read by nothing outside
 * this capability and its panel — the `validation.ts` / `usage.ts` precedent, and
 * exactly the condition CLAUDE.md puts on a store that is not a slice of
 * `store.ts`.
 */

import type { DockviewApi, IDockviewPanelProps } from "dockview";
import { createElement, lazy, Suspense, type ReactElement } from "react";
import { create } from "zustand";

import * as api from "./api";
import { openPanel } from "./dock";
import type { ToolCommand, WorkbenchTool } from "./registry";
import type { SpecOutcome, SpecState, SpecStatus, WorkspaceEvent } from "./types";
import { ReconnectingSocket } from "./ws";

/** Stable contract: a saved layout references this panel by it (`docs/tools.md`). */
export const TOOL_ID = "specs";

/** Who the approval is recorded as. The server stamps the timestamp and the
 * digest; this is the only field the client supplies, and it is deliberately a
 * constant rather than a text box — the machine's user is the only person who
 * can be at this keyboard, and a free-text approver would be a field to forge. */
const APPROVER = "you";

// ---- the semantic mapping (no new colour) -----------------------------------

/** How one spec renders: a DESIGN.md token for its colour, the matching tint,
 * and the word that goes with it — colour is never the only signal (§7). */
export interface SpecVisual {
  token: string;
  bg: string;
  label: string;
}

/** A spec's *status* — whether it may run at all. Straight onto the existing
 * semantic ramp; nothing new is invented. */
const STATUS_VISUAL: Record<SpecStatus, SpecVisual> = {
  approved: { token: "--success", bg: "--success-bg", label: "Approved" },
  // Not red: nobody has said yes yet, which is the resting state of a spec
  // somebody checked in, not a failure.
  unapproved: { token: "--info", bg: "--info-bg", label: "Not approved" },
  // The code moved under an approval. This one has to *stop* a reader, so it
  // takes the warn ramp rather than the "could not judge" grey.
  stale: { token: "--warn", bg: "--warn-bg", label: "Needs re-approval" },
  invalid: { token: "--error", bg: "--error-bg", label: "Unreadable" },
};

/** A *run's* verdict. `blocked` shares the neutral grey with `skipped`: neither
 * is a disagreement about the numbers, both are "did not / could not judge". */
const OUTCOME_VISUAL: Record<SpecOutcome, SpecVisual> = {
  pass: { token: "--success", bg: "--success-bg", label: "Pass" },
  warn: { token: "--warn", bg: "--warn-bg", label: "Warn" },
  fail: { token: "--error", bg: "--error-bg", label: "Fail" },
  blocked: { token: "--agent-idle", bg: "--agent-idle-bg", label: "Blocked" },
  skipped: { token: "--agent-idle", bg: "--agent-idle-bg", label: "Skipped" },
};

export const statusVisual = (status: SpecStatus): SpecVisual => STATUS_VISUAL[status];
export const outcomeVisual = (outcome: SpecOutcome): SpecVisual => OUTCOME_VISUAL[outcome];

// ---- ordering and the reading -----------------------------------------------

/** Worst first, then by name. The order the panel lists them in, so the row
 * that needs a person is the row they see without scrolling. */
const STATUS_RANK: Record<SpecStatus, number> = {
  invalid: 0,
  stale: 1,
  unapproved: 2,
  approved: 3,
};

export function orderSpecs(specs: readonly SpecState[]): SpecState[] {
  return [...specs].sort(
    (a, b) => STATUS_RANK[a.status] - STATUS_RANK[b.status] || a.name.localeCompare(b.name),
  );
}

/**
 * Does this spec want a person right now?
 *
 * Two ways, and they are different questions with the same answer. The *spec*
 * needs one when its code moved or its file will not parse — nothing is running,
 * and a loop that went quiet is indistinguishable from one that broke. The *run*
 * needs one when the last verdict was a fail or a refusal.
 *
 * A spec nobody has approved yet is deliberately **not** counted: it is not a
 * problem, it is a thing somebody checked in and has not armed. Counting it
 * would put a number on the bar of every workspace that ever contained a spec.
 */
export function needsAttention(spec: SpecState): boolean {
  if (spec.status === "stale" || spec.status === "invalid") return true;
  const outcome = spec.last_run?.outcome;
  return outcome === "fail" || outcome === "blocked";
}

/** How many specs need a person — the status-bar reading, which hides at zero
 * (DESIGN §6.7). A quiet bar means nothing needs you. */
export function attentionCount(specs: readonly SpecState[]): number {
  return specs.filter(needsAttention).length;
}

/** One line for the chip's tooltip. Names the specs rather than only counting
 * them: a number with no noun is a thing you have to click to understand. */
export function attentionTitle(specs: readonly SpecState[]): string {
  const wanting = orderSpecs(specs.filter(needsAttention));
  if (wanting.length === 0) return "Every reconciliation spec is passing";
  const named = wanting.slice(0, 3).map((spec) => `${spec.name} (${summaryOf(spec)})`);
  const more = wanting.length > 3 ? `, and ${String(wanting.length - 3)} more` : "";
  return `${named.join(", ")}${more}`;
}

/** The shortest true thing about a spec — the word a row's pill carries. */
export function summaryOf(spec: SpecState): string {
  if (spec.status !== "approved") return STATUS_VISUAL[spec.status].label.toLowerCase();
  const outcome = spec.last_run?.outcome;
  return outcome === undefined ? "not run yet" : OUTCOME_VISUAL[outcome].label.toLowerCase();
}

// ---- the store ---------------------------------------------------------------

interface SpecStore {
  /** Held specs, keyed by `name`. Empty is a real answer (a workspace with no
   * specs) and the common one — never a singleton. */
  specs: Record<string, SpecState>;
  /** The server's sentence about the *set* — what an empty list means, which is
   * a thing blankness cannot say (AXI shape 2, applied to a person). */
  detail: string;
  /** `false` until the first `refresh()` settles either way. Before it flips, an
   * absent name is *unknown*, not gone — the `validation.ts` posture. */
  hydrated: boolean;
  /** The last call that failed, as one sentence. Cleared by the next attempt. */
  error: string | null;
  init: () => void;
  refresh: () => Promise<void>;
  /** The one-time trust decision, echoing the digest this row was rendered with.
   * A 409 (the spec moved under the dialog) surfaces as `error` and re-reads,
   * never as a decision that silently did not land. */
  approve: (name: string) => Promise<void>;
  revoke: (name: string) => Promise<void>;
  /** Run one now. The loop's point is that nobody has to, so this is the
   * affordance for the first run after an approval and for impatience. */
  run: (name: string) => Promise<void>;
}

let started = false;

/** Reset the module-level "already subscribed" latch and the map. Tests only. */
export function resetSpecStoreForTests(): void {
  started = false;
  useSpecStore.setState({ specs: {}, detail: "", hydrated: false, error: null });
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export const useSpecStore = create<SpecStore>((set, get) => ({
  specs: {},
  detail: "",
  hydrated: false,
  error: null,

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "reconcile_spec") return;
        // This frame is the whole product claim arriving: a run nobody fired,
        // pushed to a window that never asked for it.
        set((state) => ({ specs: { ...state.specs, [event.state.name]: event.state } }));
      },
      // Every reconnect re-reads: a run that happened while the socket was down
      // is gone from the live stream, and the load path is the only way back.
      onOpen: () => {
        void get().refresh();
      },
    });
  },

  refresh: async () => {
    try {
      const { specs, detail } = await api.getSpecs();
      set({
        specs: Object.fromEntries(specs.map((spec) => [spec.name, spec])),
        detail,
        hydrated: true,
        error: null,
      });
    } catch (error) {
      // A server that cannot answer leaves the last map standing — the honest
      // reading, and the usage/activity posture. Still hydrated: the store
      // *answered*, so an absent name is now genuinely absent.
      set({ hydrated: true, error: message(error) });
    }
  },

  approve: async (name) => {
    const spec = get().specs[name];
    if (spec === undefined) return;
    set({ error: null });
    try {
      const updated = await api.approveSpec(name, {
        approver: APPROVER,
        digest: spec.digest,
      });
      set((state) => ({ specs: { ...state.specs, [updated.name]: updated } }));
    } catch (error) {
      // Whatever went wrong, the row on screen is now a row nobody should act
      // on again until it has been re-read — the 409 case *is* "your copy is
      // stale". Re-read first and set the message after, because `refresh`
      // clears the error on success and the user has to keep the sentence that
      // told them why their click did nothing.
      await get().refresh();
      set({ error: message(error) });
    }
  },

  revoke: async (name) => {
    set({ error: null });
    try {
      const updated = await api.revokeSpec(name);
      set((state) => ({ specs: { ...state.specs, [updated.name]: updated } }));
    } catch (error) {
      set({ error: message(error) });
    }
  },

  run: async (name) => {
    set({ error: null });
    try {
      // The report also arrives on the socket as a whole `SpecState`; re-reading
      // is what turns it into the row, and it is idempotent.
      await api.runSpec(name);
      await get().refresh();
    } catch (error) {
      set({ error: message(error) });
    }
  },
}));

// ---- lifecycle ---------------------------------------------------------------

/** The panel body's chunk. `React.lazy` caches the import, so warming it means
 * the panel mounts without suspending. Idempotent and cheap once warm. */
async function loadPanel(): Promise<void> {
  try {
    await import("./panels/SpecPanel");
  } catch {
    // The panel opens on the Suspense fallback instead; not worth blocking on.
  }
}

function openSpecs(): void {
  useSpecStore.getState().init();
  void useSpecStore.getState().refresh();
  void loadPanel().then(() => {
    openPanel(TOOL_ID);
  });
}

function warmPanel(): void {
  const warm = (): void => void loadPanel();
  const idle = (globalThis as { requestIdleCallback?: (cb: () => void) => number })
    .requestIdleCallback;
  if (idle === undefined) setTimeout(warm, 1_000);
  else idle(warm);
}

function onDockReady(dock: DockviewApi | null): void {
  if (dock === null) return;
  useSpecStore.getState().init();
  warmPanel();
}

/** The workspace moved: a different project has different specs, and different
 * approvals. Re-read rather than carry — a row about `dispatch` in the folder
 * you left is not a row about `dispatch` in the one you opened. */
function onWorkspaceChanged(): void {
  useSpecStore.setState({ specs: {}, hydrated: false, error: null });
  void useSpecStore.getState().refresh();
}

// ---- the panel wrapper and the chip (no JSX: see the module note) ------------

const SpecPanelLazy = lazy(() => import("./panels/SpecPanel"));

function SpecsPanel(props: IDockviewPanelProps): ReactElement {
  return createElement(
    Suspense,
    { fallback: createElement("div", { className: "wb-spec-loading u-label" }, "Loading…") },
    createElement(SpecPanelLazy, props),
  );
}

/** The tab glyph: a workbook grid with a tick — the two ideas this capability
 * joins, and deliberately not the Review panel's shield. */
function SpecsIcon(): ReactElement {
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
      strokeLinejoin: "round" as const,
      "aria-hidden": true,
    },
    createElement("rect", { key: "grid", x: 2, y: 2.5, width: 8, height: 11, rx: 1 }),
    createElement("path", { key: "rows", d: "M2 6h8M6 2.5v11" }),
    createElement("path", { key: "tick", d: "M9.5 11l2 2 3.2-4" }),
  );
}

/**
 * The status chip. Hidden at zero, per the quiet-bar doctrine — and that is the
 * whole point of this capability: a bar with nothing on it is a bar saying every
 * workbook still agrees with the code.
 */
export function SpecStatusChip(): ReactElement | null {
  const specs = useSpecStore((state) => state.specs);
  const rows = Object.values(specs);
  const count = attentionCount(rows);
  if (count === 0) return null;
  return createElement(
    "button",
    {
      type: "button",
      className: "wb-status-chip",
      title: attentionTitle(rows),
      onClick: openSpecs,
    },
    createElement("span", { key: "glyph", "aria-hidden": true }, "⌗"),
    createElement("span", { key: "count", className: "u-tabular" }, String(count)),
    createElement("span", { key: "noun" }, count === 1 ? "spec" : "specs"),
  );
}

// ---- registration -------------------------------------------------------------

const commands: readonly ToolCommand[] = [
  {
    id: "specs.open",
    title: "Reconciliation specs…",
    detail: () => "approve a spec once; every save of its workbook re-runs the gate",
    run: openSpecs,
  },
];

export const specsTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Specs",
  icon: SpecsIcon,
  panel: {
    component: SpecsPanel,
    // Right, beside Review: the two are the same idea at two distances — this
    // one says which proofs are armed, that one says what one proof found.
    defaultLocation: { area: "right", size: 340 },
    openByDefault: false,
    // SINGULAR, and here is the reason the pane rules ask for. A pane of this
    // tool points at no resource: it is the *list* of the workspace's specs, of
    // which there is exactly one, and a second pane would be a second view of
    // the same rows each able to approve over the other's decision. The plural
    // surface this capability really has is the Review pane — one per
    // `ValidationResult`, which is what a run produces.
    singleton: true,
  },
  commands,
  // No Alt chord by default (the Review/Workspaces precedent): a registered
  // chord is one the user's own `shortcuts.md` can never have, and approving a
  // spec is not a reflex. It is bindable from `shortcuts.md` the day it lands.
  statusContributions: [{ region: "right", component: SpecStatusChip }],
  onDockReady,
  onWorkspaceChanged,
};
