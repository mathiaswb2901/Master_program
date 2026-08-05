/**
 * Reconciling the two sources the provenance map has.
 *
 * `GET /api/provenance` returns a whole snapshot (on load, and on every socket
 * reconnect); `file_provenance` frames arrive one path at a time on the same
 * socket. Both feed the same map, and the GET is not instantaneous — a frame
 * that lands while it is in flight describes a *later* state than the snapshot
 * does, so replacing the map wholesale would undo it.
 *
 * That is not hypothetical: `onOpen` fires the subscription and the GET
 * together, so a reconnect after a laptop wakes is exactly when a backlog of
 * frames and this request coincide. Undoing a clear there restores an
 * attribution the server has already retracted, and no further event is coming
 * to correct it.
 */

import type { ProvenanceEntry } from "./types";

export type ProvenanceMapState = Record<string, ProvenanceEntry>;

/**
 * The map after a snapshot lands: the snapshot, except that every path a socket
 * frame touched while the request was in flight keeps its live value — present
 * for an attribution, absent for a clear.
 */
export function applyProvenanceSnapshot(
  snapshot: readonly ProvenanceEntry[],
  live: ProvenanceMapState,
  touchedWhileInFlight: ReadonlySet<string>,
): ProvenanceMapState {
  const next: ProvenanceMapState = {};
  for (const entry of snapshot) {
    if (!touchedWhileInFlight.has(entry.path)) next[entry.path] = entry;
  }
  for (const path of touchedWhileInFlight) {
    const current = live[path];
    if (current !== undefined) next[path] = current;
  }
  return next;
}

/**
 * Dismissals worth keeping: the ones for paths the snapshot still attributes.
 *
 * Dismissal outlives a reload, so without this the set would only ever grow —
 * a server restart forgets every attribution, and the paths it forgot would sit
 * in storage for good. Returns the original object when nothing was dropped, so
 * callers can persist on identity change alone.
 */
export function prunedDismissed(
  dismissed: Record<string, boolean>,
  snapshot: readonly ProvenanceEntry[],
): Record<string, boolean> {
  const attributed = new Set(snapshot.map((entry) => entry.path));
  const kept: Record<string, boolean> = {};
  for (const path of Object.keys(dismissed)) {
    if (dismissed[path] === true && attributed.has(path)) kept[path] = true;
  }
  return Object.keys(kept).length === Object.keys(dismissed).length ? dismissed : kept;
}
