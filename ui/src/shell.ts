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
 * Register the native close guard; the returned function unregisters it.
 *
 * The shell arms its guard only once this resolves (`shell_ready`), so a
 * webview that never ran this code still closes normally instead of leaving an
 * unclosable window.
 */
export async function onCloseRequested(handler: () => void): Promise<Unlisten> {
  if (!isTauri()) return noop;
  try {
    const { listen } = await import("@tauri-apps/api/event");
    const unlisten = await listen(CLOSE_REQUESTED_EVENT, () => handler());
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
