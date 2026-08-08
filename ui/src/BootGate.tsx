/**
 * The launch loading state, shown while `backendReady && tokenReady` is still
 * false (`App.tsx`).
 *
 * It is a smooth continuation of the inline pre-JS splash (index.html): the same
 * mark, wordmark and status line, drawn here from the ANVIL tokens rather than
 * the splash's hard-coded literals, so when App removes the overlay there is no
 * flash or jump. The copy advances honestly as the wait stretches — see
 * `boot.ts` for the phases and their timing — and a wait that never resolves
 * ends in an error with a Retry, never an endless spinner.
 */

import { useEffect, useState } from "react";

import { BOOT_ENGINE_MS, BOOT_TIMEOUT_MS, type BootPhase, bootStatus } from "./boot";

export function BootGate(): JSX.Element {
  const [phase, setPhase] = useState<BootPhase>("starting");

  useEffect(() => {
    // Two one-shot timers from mount, at the phase thresholds `boot.ts` defines.
    // The engine copy only advances a wait still on the first phase; the timeout
    // always wins, because reaching it means the backend never answered.
    const toEngine = window.setTimeout(
      () => setPhase((p) => (p === "starting" ? "engine" : p)),
      BOOT_ENGINE_MS,
    );
    const toFailed = window.setTimeout(() => setPhase("failed"), BOOT_TIMEOUT_MS);
    return () => {
      window.clearTimeout(toEngine);
      window.clearTimeout(toFailed);
    };
  }, []);

  if (phase === "failed") {
    return (
      <div className="wb-boot" role="alert">
        <div className="wb-boot-inner">
          <div className="wb-boot-mark is-error" aria-hidden="true" />
          <div className="wb-boot-name">Workbench</div>
          <div className="wb-boot-status">{bootStatus(phase)}</div>
          <button
            type="button"
            className="wb-btn wb-boot-retry"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="wb-boot" role="status">
      <div className="wb-boot-inner">
        <div className="wb-boot-mark" aria-hidden="true" />
        <div className="wb-boot-name">Workbench</div>
        <div className="wb-boot-status">{bootStatus(phase)}</div>
      </div>
    </div>
  );
}
