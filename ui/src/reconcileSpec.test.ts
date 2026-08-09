/**
 * Spec-from-code on the UI side: the pure rules, the held map, and the trust
 * round trip (productivity loops, PR-B).
 *
 * Three claims are tested rather than trusted, because each is a promise a later
 * edit could quietly break:
 *
 * - **Approve echoes the digest the row was rendered with.** Not the freshest
 *   one the store could fetch — the one on screen. Approving a spec whose bytes
 *   moved under the dialog would approve something nobody read, and the only
 *   place that can go wrong is here.
 * - **A 409 is not a decision.** It surfaces as an error *and* re-reads, so the
 *   row a user acts on next is one the server just described.
 * - **The bar stays quiet.** `attentionCount` counts the specs that need a
 *   person and deliberately does not count the ones nobody has armed yet: a
 *   number on the bar for every workspace that ever contained a spec is a bar
 *   nobody reads.
 *
 * The server half — the approval, the composite digest, the watcher loop — is
 * `server/tests/test_reconcile_spec.py`, which is also where the save→chip-flip
 * moment is driven end to end.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The REST client: the real `ApiError` (so `instanceof` holds), stubbed calls.
// Hoisted, because the `vi.mock` factory is lifted above every import.
const { approveSpec, getSpecs, revokeSpec, runSpec } = vi.hoisted(() => ({
  approveSpec: vi.fn(),
  getSpecs: vi.fn(),
  revokeSpec: vi.fn(),
  runSpec: vi.fn(),
}));
vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, approveSpec, getSpecs, revokeSpec, runSpec };
});

// The dock, stubbed: the real one pulls in `tools.ts` and therefore every tool
// module — including the store, which reads `document` at import. None of that
// is what is under test here (the `registry.test.ts` precedent).
vi.mock("./dock", () => ({ openPanel: vi.fn() }));

import { ApiError } from "./api";
import {
  attentionCount,
  attentionTitle,
  needsAttention,
  orderSpecs,
  outcomeVisual,
  resetSpecStoreForTests,
  statusVisual,
  summaryOf,
  useSpecStore,
} from "./reconcileSpec";
import type { SpecOutcome, SpecRunReport, SpecState, SpecStatus } from "./types";

const ALL_STATUSES: SpecStatus[] = ["unapproved", "approved", "stale", "invalid"];
const ALL_OUTCOMES: SpecOutcome[] = ["pass", "warn", "fail", "blocked", "skipped"];

function run(outcome: SpecOutcome, patch: Partial<SpecRunReport> = {}): SpecRunReport {
  return {
    name: "dispatch",
    outcome,
    validation_id: "val_1",
    trigger: "watcher",
    ran_at: "2026-08-09T14:02:11",
    duration_ms: 42,
    values: 3,
    detail: `${outcome} detail`,
    ...patch,
  };
}

function spec(name: string, patch: Partial<SpecState> = {}): SpecState {
  return {
    name,
    path: `.workbench/reconcile/${name}.toml`,
    workbook: "models/budget.xlsx",
    status: "approved",
    digest: `digest-${name}`,
    checks: 1,
    approval: null,
    pending_covered: [],
    last_run: null,
    detail: "detail",
    ...patch,
  };
}

afterEach(() => {
  vi.clearAllMocks();
  resetSpecStoreForTests();
});

// ---- the semantic mapping ---------------------------------------------------

describe("the semantic mapping", () => {
  it("covers every status and every outcome", () => {
    for (const status of ALL_STATUSES) expect(statusVisual(status).label).toBeTruthy();
    for (const outcome of ALL_OUTCOMES) expect(outcomeVisual(outcome).label).toBeTruthy();
  });

  it("invents no colour — every token is one the semantic ramp already has", () => {
    const allowed = new Set(["--success", "--info", "--warn", "--error", "--agent-idle"]);
    for (const status of ALL_STATUSES) expect(allowed.has(statusVisual(status).token)).toBe(true);
    for (const outcome of ALL_OUTCOMES) expect(allowed.has(outcomeVisual(outcome).token)).toBe(true);
  });

  it("blocked shares the neutral grey with skipped, not the red of a fail", () => {
    // Neither is a disagreement about the numbers; both are "could not judge".
    expect(outcomeVisual("blocked").token).toBe(outcomeVisual("skipped").token);
    expect(outcomeVisual("blocked").token).not.toBe(outcomeVisual("fail").token);
  });

  it("a stale spec is a warn, not a grey — it has to stop a reader", () => {
    expect(statusVisual("stale").token).toBe("--warn");
  });
});

// ---- ordering and the reading ----------------------------------------------

describe("ordering", () => {
  it("puts the rows that need a person first, then sorts by name", () => {
    const rows = orderSpecs([
      spec("z-ok"),
      spec("a-ok"),
      spec("broken", { status: "invalid" }),
      spec("moved", { status: "stale" }),
      spec("new", { status: "unapproved" }),
    ]);
    expect(rows.map((row) => row.name)).toEqual(["broken", "moved", "new", "a-ok", "z-ok"]);
  });
});

describe("attention", () => {
  it("counts a spec whose code moved and one whose last run failed", () => {
    expect(needsAttention(spec("a", { status: "stale" }))).toBe(true);
    expect(needsAttention(spec("b", { status: "invalid" }))).toBe(true);
    expect(needsAttention(spec("c", { last_run: run("fail") }))).toBe(true);
    expect(needsAttention(spec("d", { last_run: run("blocked") }))).toBe(true);
  });

  it("does not count a spec nobody has armed yet", () => {
    // A checked-in spec nobody approved is not a problem; it is a thing waiting
    // for a decision. Counting it would put a number on the bar of every
    // workspace that ever contained one.
    expect(needsAttention(spec("new", { status: "unapproved" }))).toBe(false);
  });

  it("does not count a passing or warning run", () => {
    expect(needsAttention(spec("a", { last_run: run("pass") }))).toBe(false);
    expect(needsAttention(spec("b", { last_run: run("warn") }))).toBe(false);
  });

  it("the reading hides at zero — a quiet bar means nothing needs you", () => {
    expect(attentionCount([spec("a"), spec("b", { status: "unapproved" })])).toBe(0);
  });

  it("the tooltip names the specs rather than only counting them", () => {
    const title = attentionTitle([spec("dispatch", { last_run: run("fail") }), spec("ok")]);
    expect(title).toContain("dispatch");
    expect(title).toContain("fail");
  });

  it("the tooltip says so when everything passes", () => {
    expect(attentionTitle([spec("ok")])).toContain("passing");
  });

  it("the tooltip caps the names and says how many more there are", () => {
    const many = ["a", "b", "c", "d", "e"].map((name) => spec(name, { status: "stale" }));
    expect(attentionTitle(many)).toContain("2 more");
  });
});

describe("summaryOf", () => {
  it("reports the status when the spec cannot run, and the run when it can", () => {
    expect(summaryOf(spec("a", { status: "stale" }))).toBe("needs re-approval");
    expect(summaryOf(spec("b", { status: "unapproved" }))).toBe("not approved");
    expect(summaryOf(spec("c"))).toBe("not run yet");
    expect(summaryOf(spec("d", { last_run: run("fail") }))).toBe("fail");
  });
});

// ---- the store --------------------------------------------------------------

describe("useSpecStore", () => {
  beforeEach(() => {
    getSpecs.mockResolvedValue({ specs: [], detail: "No reconciliation specs." });
  });

  it("refresh keys the map by name and keeps the server's sentence about the set", async () => {
    getSpecs.mockResolvedValue({ specs: [spec("dispatch")], detail: "1 spec(s): 1 approved." });
    await useSpecStore.getState().refresh();
    expect(useSpecStore.getState().specs.dispatch?.workbook).toBe("models/budget.xlsx");
    expect(useSpecStore.getState().detail).toBe("1 spec(s): 1 approved.");
    expect(useSpecStore.getState().hydrated).toBe(true);
  });

  it("a server that cannot answer leaves the last map standing and still hydrates", async () => {
    useSpecStore.setState({ specs: { dispatch: spec("dispatch") } });
    getSpecs.mockRejectedValue(new ApiError(500, "boom"));
    await useSpecStore.getState().refresh();
    expect(useSpecStore.getState().specs.dispatch).toBeDefined();
    expect(useSpecStore.getState().hydrated).toBe(true);
    expect(useSpecStore.getState().error).toBe("boom");
  });

  it("approve echoes the digest the row was rendered with", async () => {
    const row = spec("dispatch", { status: "unapproved", digest: "as-rendered" });
    useSpecStore.setState({ specs: { dispatch: row } });
    approveSpec.mockResolvedValue({ ...row, status: "approved" });

    await useSpecStore.getState().approve("dispatch");

    // Not "the freshest digest the store could fetch": the one on screen. That
    // is what makes the server's 409 a real guard rather than a formality.
    expect(approveSpec).toHaveBeenCalledWith("dispatch", {
      approver: "you",
      digest: "as-rendered",
    });
    expect(useSpecStore.getState().specs.dispatch?.status).toBe("approved");
  });

  it("approving a spec the store does not hold calls nothing", async () => {
    await useSpecStore.getState().approve("ghost");
    expect(approveSpec).not.toHaveBeenCalled();
  });

  it("a 409 is surfaced as an error and the row is re-read, never silently dropped", async () => {
    useSpecStore.setState({ specs: { dispatch: spec("dispatch", { status: "unapproved" }) } });
    approveSpec.mockRejectedValue(new ApiError(409, "dispatch changed since it was shown"));
    getSpecs.mockResolvedValue({
      specs: [spec("dispatch", { status: "unapproved", digest: "moved" })],
      detail: "1 spec(s): 1 unapproved.",
    });

    await useSpecStore.getState().approve("dispatch");

    expect(useSpecStore.getState().error).toContain("changed since it was shown");
    // …and the row a user acts on next is one the server just described.
    expect(getSpecs).toHaveBeenCalled();
    expect(useSpecStore.getState().specs.dispatch?.digest).toBe("moved");
  });

  it("revoke replaces the row with the server's answer", async () => {
    useSpecStore.setState({ specs: { dispatch: spec("dispatch") } });
    revokeSpec.mockResolvedValue(spec("dispatch", { status: "unapproved" }));
    await useSpecStore.getState().revoke("dispatch");
    expect(useSpecStore.getState().specs.dispatch?.status).toBe("unapproved");
  });

  it("run re-reads afterwards, because the row is what a run changes", async () => {
    useSpecStore.setState({ specs: { dispatch: spec("dispatch") } });
    runSpec.mockResolvedValue(run("fail"));
    getSpecs.mockResolvedValue({
      specs: [spec("dispatch", { last_run: run("fail") })],
      detail: "1 spec(s): 1 approved.",
    });
    await useSpecStore.getState().run("dispatch");
    expect(useSpecStore.getState().specs.dispatch?.last_run?.outcome).toBe("fail");
  });

  it("a failing run surfaces the error and leaves the map alone", async () => {
    useSpecStore.setState({ specs: { dispatch: spec("dispatch") } });
    runSpec.mockRejectedValue(new ApiError(500, "the server fell over"));
    await useSpecStore.getState().run("dispatch");
    expect(useSpecStore.getState().error).toBe("the server fell over");
    expect(useSpecStore.getState().specs.dispatch?.last_run).toBeNull();
  });
});
