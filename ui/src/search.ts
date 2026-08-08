/**
 * Content search on the UI side: the query call, and jumping to a hit.
 *
 * The panel (`panels/Search.tsx`) holds its own query and results in local
 * component state — a search pane points at no resource, so there is nothing to
 * key a shared store by, and two panes are independent by construction. This
 * module is the part that is *not* the panel: the open-at-line jump, which is
 * shared machinery (any hit row calls it) and touches Monaco, so it lives here
 * rather than in the component.
 *
 * **Opening a hit at its line stays out of the editor's lane.** The editor panel
 * and its Monaco theme are a separate capability; this reaches the running editor
 * through the *public* Monaco API (`loadMonaco()` from `monaco.ts`, already the
 * one dynamic door onto the bundle) — never the editor component or the theme
 * wiring. It opens the file the ordinary way (`store.openFile`), then finds the
 * editor now showing that file and reveals the line. A short poll bridges the gap
 * between "the file is open" and "its editor has mounted", because the editor is
 * created asynchronously behind a `<Suspense>`; if the editor never appears (a
 * kind with no Monaco view) the open still happened and the reveal is a no-op.
 */

import { editorPathProp, loadMonaco } from "./monaco";
import { useStore } from "./store";

/** Attempts to catch the editor mounting, ~50 ms apart — 2 s total. The editor
 * chunk is usually warm by the time a user is searching, so this almost always
 * resolves on the first tick; the budget is for a cold first open. */
const REVEAL_TRIES = 40;
const REVEAL_INTERVAL_MS = 50;

const delay = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Open a file and reveal one line in the editor showing it.
 *
 * The line is 1-based (the server's own numbering). Opening is awaited so the
 * tab exists before we look for its editor; the reveal is best-effort and never
 * throws — a hit whose file cannot host a Monaco view (or an editor that never
 * mounts) still leaves the file open, which is the part that matters.
 */
export async function openHit(path: string, line: number): Promise<void> {
  await useStore.getState().openFile(path);
  try {
    await revealLine(path, line);
  } catch {
    // The file is open regardless; a reveal that could not land is not a failure
    // worth surfacing — the user is looking at the file they clicked.
  }
}

async function revealLine(path: string, line: number): Promise<void> {
  const monaco = await loadMonaco();
  const wanted = monaco.Uri.parse(editorPathProp(path)).toString();
  for (let attempt = 0; attempt < REVEAL_TRIES; attempt += 1) {
    const editor = monaco.editor
      .getEditors()
      .find((candidate) => candidate.getModel()?.uri.toString() === wanted);
    if (editor !== undefined) {
      editor.revealLineInCenter(line);
      editor.setPosition({ lineNumber: line, column: 1 });
      editor.focus();
      return;
    }
    await delay(REVEAL_INTERVAL_MS);
  }
}
