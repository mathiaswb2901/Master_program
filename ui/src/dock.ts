/**
 * The live dockview handle, and the registry-driven operations over it.
 *
 * Split out of `commands.ts` so a panel can send focus somewhere without
 * importing the command registry (and so the registry's pure derivations stay
 * free of dockview's runtime). One handle, set once by `App.tsx`.
 *
 * This file names no capability either. A tool that needs the dock itself —
 * the layout system is the one — declares `onDockReady` on its descriptor and
 * is handed the api from here, so the wiring is a registry fact rather than a
 * call `App.tsx` has to remember to make.
 */

import type { DockviewApi } from "dockview";

import { applyDefaultLayout, notifyDockReady, openToolPanel } from "./registry";
import { TOOLS } from "./tools";

let dockApi: DockviewApi | null = null;

export function setDockApi(api: DockviewApi | null): void {
  dockApi = api;
  notifyDockReady(TOOLS, api);
}

/** The live handle, for a tool that operates on the dock rather than in it. */
export function dockApiHandle(): DockviewApi | null {
  return dockApi;
}

/** Bring a panel forward — e.g. the file bar's "open this session" link, which
 * has to reach the Agent panel to be of any use. */
export function focusPanel(id: string): void {
  dockApi?.getPanel(id)?.api.setActive();
}

/** Build the startup layout from the registry. */
export function layoutDefaultPanels(api: DockviewApi): void {
  applyDefaultLayout(api, TOOLS);
}

/** Open (or focus) a registered panel that is not in the startup layout. */
export function openPanel(toolId: string): void {
  if (dockApi !== null) openToolPanel(dockApi, TOOLS, toolId);
}

/** Close a panel if it is open — the way a self-dismissing surface (the Setup
 * walkthrough) retires its own tab. A no-op when the panel is not open, so a
 * dismiss that races the tab already being closed is harmless. Names no
 * capability: it takes the panel id the caller already owns. */
export function closePanel(id: string): void {
  dockApi?.getPanel(id)?.api.close();
}
