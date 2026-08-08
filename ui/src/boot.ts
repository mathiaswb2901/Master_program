/**
 * The boot gate's copy and timing — the pure half of the launch loading state.
 *
 * Kept apart from `BootGate.tsx` so it is testable in the node vitest environment
 * (no DOM): the component is a thin shell over these three functions, and the
 * phase transitions a user sees are exactly what `bootPhaseAt` decides.
 *
 * The gate reuses the app's existing `backendReady` signal (`App.tsx`); these
 * timers only decide *what the wait looks like*, never *whether* it is over.
 * While the backend is booting we show honest, progressive copy; if it never
 * answers within a generous window we stop pretending and offer a retry rather
 * than spin forever.
 */

export type BootPhase = "starting" | "engine" | "failed";

/** After this long the copy owns up to what the wait is: the local engine. */
export const BOOT_ENGINE_MS = 4_000;

/** After this long with no backend we give up on the spinner and offer retry.
 * Generous on purpose — a cold frozen-Python start on a slow disk is seconds,
 * not tens of them, so a timeout here means something is actually wrong. */
export const BOOT_TIMEOUT_MS = 30_000;

/** The phase a boot of `elapsedMs` is in. Monotonic in `elapsedMs`. */
export function bootPhaseAt(elapsedMs: number): BootPhase {
  if (elapsedMs >= BOOT_TIMEOUT_MS) return "failed";
  if (elapsedMs >= BOOT_ENGINE_MS) return "engine";
  return "starting";
}

/** The status line shown under the wordmark for each phase. */
export function bootStatus(phase: BootPhase): string {
  switch (phase) {
    case "starting":
      // Matches the inline splash in index.html verbatim, so the hand-off from
      // the pre-JS overlay to React shows no text change.
      return "Starting Workbench…";
    case "engine":
      return "Starting the local engine…";
    case "failed":
      return "Workbench could not reach its local engine.";
  }
}
