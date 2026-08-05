import { describe, expect, it } from "vitest";

import { applyProvenanceSnapshot, prunedDismissed } from "./provenance";
import type { ProvenanceEntry } from "./types";

const entry = (path: string, acknowledged = false): ProvenanceEntry => ({
  path,
  changed_at: 1_700_000_000,
  agent: { session_id: "s1", session_title: "Fix the bidder", tool: "Write" },
  acknowledged,
});

describe("applyProvenanceSnapshot", () => {
  it("takes the snapshot when no event raced it", () => {
    const next = applyProvenanceSnapshot([entry("a.py"), entry("b.py")], {}, new Set());
    expect(Object.keys(next).sort()).toEqual(["a.py", "b.py"]);
  });

  it("drops what the snapshot no longer has", () => {
    const next = applyProvenanceSnapshot([entry("a.py")], { "gone.py": entry("gone.py") }, new Set());
    expect(next["gone.py"]).toBeUndefined();
  });

  it("keeps a clear that landed while the request was in flight", () => {
    // The server retracted b.py after answering the GET: restoring the stale
    // snapshot entry would leave a false attribution with no event coming.
    const next = applyProvenanceSnapshot(
      [entry("a.py"), entry("b.py")],
      { "a.py": entry("a.py") },
      new Set(["b.py"]),
    );
    expect(next["b.py"]).toBeUndefined();
    expect(next["a.py"]).toEqual(entry("a.py"));
  });

  it("keeps a fresh attribution that landed while the request was in flight", () => {
    const fresh = entry("c.py");
    const next = applyProvenanceSnapshot([entry("a.py")], { "c.py": fresh }, new Set(["c.py"]));
    expect(next["c.py"]).toEqual(fresh);
  });

  it("prefers the live entry over the snapshot's older copy of the same path", () => {
    const live = entry("a.py", true);
    const next = applyProvenanceSnapshot([entry("a.py", false)], { "a.py": live }, new Set(["a.py"]));
    expect(next["a.py"]?.acknowledged).toBe(true);
  });
});

describe("prunedDismissed", () => {
  it("keeps dismissals for paths that are still attributed", () => {
    const dismissed = { "a.py": true };
    expect(prunedDismissed(dismissed, [entry("a.py")])).toBe(dismissed);
  });

  it("drops dismissals the server no longer attributes", () => {
    expect(prunedDismissed({ "a.py": true, "b.py": true }, [entry("a.py")])).toEqual({
      "a.py": true,
    });
  });
});
