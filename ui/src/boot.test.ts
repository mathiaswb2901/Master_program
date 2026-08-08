/**
 * The boot gate's pure timing and copy (`boot.ts`) — the phase sequence a user
 * sees while the backend boots, tested without a DOM. `Boot.tsx` is a thin shell
 * over these, and the e2e launch spec proves the rendered hand-off; here we pin
 * the decisions.
 */

import { describe, expect, it } from "vitest";

import { BOOT_ENGINE_MS, BOOT_TIMEOUT_MS, bootPhaseAt, bootStatus } from "./boot";

describe("boot phase timing", () => {
  it("starts on the calm 'starting' phase", () => {
    expect(bootPhaseAt(0)).toBe("starting");
    expect(bootPhaseAt(BOOT_ENGINE_MS - 1)).toBe("starting");
  });

  it("advances to progressive 'engine' copy after a few seconds", () => {
    expect(bootPhaseAt(BOOT_ENGINE_MS)).toBe("engine");
    expect(bootPhaseAt(BOOT_TIMEOUT_MS - 1)).toBe("engine");
  });

  it("falls to the failed/retry state once the generous timeout passes", () => {
    expect(bootPhaseAt(BOOT_TIMEOUT_MS)).toBe("failed");
    expect(bootPhaseAt(BOOT_TIMEOUT_MS + 60_000)).toBe("failed");
  });

  it("keeps the thresholds ordered so 'engine' is always shown before 'failed'", () => {
    expect(BOOT_ENGINE_MS).toBeLessThan(BOOT_TIMEOUT_MS);
  });
});

describe("boot copy", () => {
  it("opens with the exact line the inline splash paints, for a seamless hand-off", () => {
    // Must match index.html's .wb-splash-status verbatim.
    expect(bootStatus("starting")).toBe("Starting Workbench…");
  });

  it("names the local engine once the wait stretches", () => {
    expect(bootStatus("engine")).toContain("local engine");
  });

  it("is honest, not a spinner, when the backend never answers", () => {
    expect(bootStatus("failed")).toContain("could not reach");
  });
});
