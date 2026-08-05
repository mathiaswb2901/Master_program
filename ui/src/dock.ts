/**
 * The live dockview handle, and the registry-driven operations over it.
 *
 * Split out of `commands.ts` so a panel can send focus somewhere without
 * importing the command registry (and so the registry's pure derivations stay
 * free of dockview's runtime). One handle, set once by `App.tsx`.
 */

import type { DockviewApi } from "dockview";

import { applyDefaultLayout, openToolPanel } from "./registry";
import { TOOLS } from "./tools";

let dockApi: DockviewApi | null = null;

export function setDockApi(api: DockviewApi | null): void {
  dockApi = api;
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
