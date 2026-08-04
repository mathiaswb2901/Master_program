/**
 * The seam that lets anything type into a live terminal.
 *
 * Each TerminalInstance owns its own PTY socket and xterm instance; it
 * registers a handle here for the life of its tab. The store reaches terminals
 * through this map, so nothing outside `panels/Terminal.tsx` imports xterm.
 */

export interface TerminalHandle {
  /** Bytes to the PTY, exactly as typed — no newline is ever added. */
  send: (data: string) => void;
  focus: () => void;
}

const handles = new Map<number, TerminalHandle>();

export function registerTerminal(id: number, handle: TerminalHandle): () => void {
  handles.set(id, handle);
  return () => {
    // A remount (Reconnect) registers the new handle before the old one tears
    // down; only drop the entry if it is still ours.
    if (handles.get(id) === handle) handles.delete(id);
  };
}

export function terminalHandle(id: number): TerminalHandle | null {
  return handles.get(id) ?? null;
}
