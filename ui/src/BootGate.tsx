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

import { BOOT_ENGINE_MS, BOOT_TIMEOUT_MS, type BootPhase, bootPhaseAt, bootStatus } from "./boot";

export function BootGate(): JSX.Element {
  const [phase, setPhase] = useState<BootPhase>(() => bootPhaseAt(0));

  useEffect(() => {
    const mountedAt = Date.now();
    // The phase a user sees is whatever `bootPhaseAt` returns for the time since
    // mount — this component owns no transition logic of its own. The two timers
    // only wake it at the moments the phase can change (the thresholds `boot.ts`
    // defines); the pure, tested function alone decides what it becomes, so the
    // rendered path and `boot.test.ts` can never disagree.
    const sync = (): void => setPhase(bootPhaseAt(Date.now() - mountedAt));
    const toEngine = window.setTimeout(sync, BOOT_ENGINE_MS);
    const toFailed = window.setTimeout(sync, BOOT_TIMEOUT_MS);
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
