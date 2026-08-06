/** Host adapter: the same UI runs in a browser tab and in the Tauri window.
 *
 * Nothing above this module branches on the host. In a browser every call here
 * is a no-op, so `npm run dev` behaves exactly as it did before the shell
 * existed. Inside Tauri the calls reach the native window, which is the only
 * place two of our behaviours can live at all:
 *
 * - `beforeunload` — WebView2 ignores it, so the shell holds `CloseRequested`
 *   and asks us instead (`onCloseRequested`).
 * - the `document.title` attention badge — a native title bar and the taskbar
 *   read the *window* title, not the DOM (`setAttention`).
 *
 * `@tauri-apps/api` is imported dynamically and only after `isTauri()` passes,
 * so a browser build never fetches the chunk.
 */

/** Tauri v2 injects this into the page before any app code runs. */
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

/** Must match `CLOSE_REQUESTED_EVENT` in `desktop/src-tauri/src/lib.rs`. */
const CLOSE_REQUESTED_EVENT = "workbench://close-requested";
/** Must match `BACKEND_READY_EVENT` in `desktop/src-tauri/src/lib.rs`. */
const BACKEND_READY_EVENT = "workbench://backend-ready";

export type Unlisten = () => void;

const noop: Unlisten = () => {};

/** One IPC call, swallowed outside the shell and never allowed to throw into a
 * React effect. A failure is visible in the console rather than fatal — the
 * worst case (a `confirm_close` that never lands) leaves the window open. */
async function invoke(command: string, args?: Record<string, unknown>): Promise<void> {
  if (!isTauri()) return;
  try {
    const { invoke: call } = await import("@tauri-apps/api/core");
    await call(command, args);
  } catch (err) {
    console.error(`shell command ${command} failed`, err);
  }
}

/**
 * Resolve once the shell has finished bringing the backend up — attached to one
 * already listening, spawned one that answered, or given up on it.
 *
 * The shell opens its window immediately and supervises on a worker thread, so
 * waiting is the UI's job. It is not optional: `/ws/events` reconnects on its
 * own, but the terminal does not, and a PTY socket that hits `ECONNREFUSED` on
 * first paint leaves the tab reading "Terminal exited" until the user clicks
 * Reconnect. In a browser the backend is whatever served this page, so this
 * resolves immediately.
 */
export async function awaitBackendReady(): Promise<void> {
  if (!isTauri()) return;
  try {
    const [{ listen }, { invoke: call }] = await Promise.all([
      import("@tauri-apps/api/event"),
      import("@tauri-apps/api/core"),
    ]);
    let settle = (): void => {};
    const settled = new Promise<void>((resolve) => {
      settle = resolve;
    });
    // Subscribe before asking: the event can fire between the two otherwise,
    // and the wait would never end.
    const unlisten = await listen(BACKEND_READY_EVENT, () => settle());
    try {
      if (await call<boolean>("backend_ready")) settle();
      await settled;
    } finally {
      unlisten();
    }
  } catch (err) {
    // Never strand the UI behind a shell call that failed.
    console.error("backend readiness could not be awaited", err);
  }
}

/**
 * Register the native close guard; the returned function unregisters it.
 *
 * The shell arms its guard only once this resolves (`shell_ready`), so a
 * webview that never ran this code still closes normally instead of leaving an
 * unclosable window. It re-arms on every page load, so this must run on each
 * one — which it does: it is a mount-time effect.
 */
export async function onCloseRequested(handler: () => void): Promise<Unlisten> {
  if (!isTauri()) return noop;
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen(CLOSE_REQUESTED_EVENT, () => {
      // Tell the shell the prompt landed. Tauri's `emit` succeeds even against
      // a page with no listener, so this ack is its only evidence that anyone
      // is home; without it the shell stops holding the window after a few
      // seconds. It gates the *watchdog*, not the user — the modal can stay up
      // as long as they like.
      void invoke("close_ack");
      handler();
    });
    await invoke("shell_ready");
    return unlisten;
  } catch (err) {
    console.error("shell close guard could not be installed", err);
    return noop;
  }
}

/** Needs-attention badge on the native window title (and so the taskbar). */
export async function setAttention(on: boolean): Promise<void> {
  await invoke("set_attention", { on });
}

/** Close for real: the user answered the dirty-close prompt with "close". */
export async function closeShellWindow(): Promise<void> {
  await invoke("confirm_close");
}

/** The user cancelled — drop the shell's pending-close state. */
export async function cancelShellClose(): Promise<void> {
  await invoke("cancel_close");
}

/**
 * Can this host show a real folder picker? False in a browser tab.
 *
 * Exposed rather than left to `isTauri()` at the call site, because the answer
 * is what the *picker* renders: outside the shell the Browse row is shown
 * disabled with the reason, not hidden, so "where did Browse go?" is never a
 * question anyone has to ask (DESIGN.md §6.5 — a row whose reason for existing
 * is that the user can see why it is unavailable).
 */
export function canPickDirectory(): boolean {
  return isTauri();
}

/**
 * The OS directory dialog. `null` = the user cancelled, or there is no shell.
 *
 * There is no browser equivalent worth shimming: `showDirectoryPicker` returns a
 * sandboxed handle, not a path, and the server needs a path it can resolve. So a
 * browser tab gets an honest fallback — type or paste a path — rather than a
 * button that does nothing. This never throws: a failed dialog is "no folder
 * chosen", which is the same thing the user pressing Escape means.
 */
export async function pickDirectory(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    return await callShell<string | null>("pick_directory");
  } catch (err) {
    console.error("folder picker failed", err);
    return null;
  }
}

/**
 * One shell command whose *answer* matters, and whose failure is the caller's
 * to explain.
 *
 * The escape hatch for capabilities that only exist natively and are not
 * behaviours of the app itself — today, docking a real Office window
 * (`officeHost.ts`). It stays here rather than importing `@tauri-apps/api`
 * somewhere else, because "the Tauri API is reached from exactly one module" is
 * the invariant that lets the browser build keep working; naming no command
 * here is what keeps this file free of capabilities.
 *
 * Rejects outside the shell instead of returning null: a caller that needs a
 * native window has no meaningful "did nothing" branch, and the office host
 * asks `isTauri()` before it ever gets here.
 */
export async function callShell<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<T> {
  if (!isTauri()) throw new Error(`${command} needs the Workbench desktop shell`);
  const { invoke: call } = await import("@tauri-apps/api/core");
  return await call<T>(command, args);
}
