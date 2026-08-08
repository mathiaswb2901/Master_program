/**
 * Validation's pure rules, the held map, and the approve round trip.
 *
 * The badge mapping, the severity threshold and the "awaiting" predicate are the
 * promises that make the surface honest, so they are unit-tested rather than
 * trusted. The store's `approve` is tested against a mocked REST client: a 200
 * reflects the recorded approval, a 404 (superseded/evicted) throws the
 * `ApiError` the panel reads as "no longer current" and drops the dead entry.
 *
 * The browser half — the panel rendering, the QuickBar command, the E2E journey
 * that drives a real result into existence — is `ReviewPanel.test.tsx` and
 * `ui/e2e/review.spec.ts`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

// The REST client: the real `ApiError` (so `instanceof` holds), stubbed calls.
// Hoisted, because the `vi.mock` factory is lifted above every import.
const { approveValidation, getValidations, runValidation } = vi.hoisted(() => ({
  approveValidation: vi.fn(),
  getValidations: vi.fn(),
  runValidation: vi.fn(),
}));
vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, approveValidation, getValidations, runValidation };
});

import { ApiError } from "./api";
import { buildCards } from "./mission";
import type { RiskLevel, ValidationResult } from "./types";
import {
  awaitingApproval,
  isMediumOrWorse,
  latestForSession,
  orderResults,
  outcomeVisual,
  resetValidationStoreForTests,
  reviewCount,
  riskVisual,
  useValidationStore,
} from "./validation";

const ALL_RISKS: RiskLevel[] = ["pass", "low", "medium", "high", "blocked"];

function result(id: string, patch: Partial<ValidationResult> = {}): ValidationResult {
  return {
    validation_id: id,
    subject: { kind: "session_output", ref: "sess-1", label: `subject ${id}` },
    risk: "pass",
    evidence: [],
    summary: "summary",
    created_at: "2026-08-08T10:00:00Z",
    completed_at: "2026-08-08T10:00:01Z",
    truncated: null,
    approval: null,
    ...patch,
  };
}

afterEach(() => {
  resetValidationStoreForTests();
  vi.clearAllMocks();
});

// ---- the badge maps onto the existing ramp, no new colour ------------------

describe("riskVisual", () => {
  it("maps each risk level onto a semantic/agent-status token — never the accent", () => {
    expect(riskVisual("pass")).toMatchObject({ token: "--success", bg: "--success-bg" });
    expect(riskVisual("low")).toMatchObject({ token: "--info", bg: "--info-bg" });
    expect(riskVisual("medium")).toMatchObject({ token: "--warn", bg: "--warn-bg" });
    expect(riskVisual("high")).toMatchObject({ token: "--error", bg: "--error-bg" });
    // The "could not judge" grey — deliberately not a red fail.
    expect(riskVisual("blocked")).toMatchObject({ token: "--agent-idle", bg: "--agent-idle-bg" });
  });

  it("every level carries a word — colour is never the only signal (§7)", () => {
    for (const risk of ALL_RISKS) expect(riskVisual(risk).label.length).toBeGreaterThan(0);
    // And nothing maps onto the amber accent, which §2.4 spends elsewhere.
    for (const risk of ALL_RISKS) expect(riskVisual(risk).token).not.toContain("accent");
  });
});

describe("outcomeVisual", () => {
  it("maps each check outcome onto a pill colour with a label", () => {
    expect(outcomeVisual("pass")).toMatchObject({ token: "--success", label: "Pass" });
    expect(outcomeVisual("warn")).toMatchObject({ token: "--warn", label: "Warn" });
    expect(outcomeVisual("fail")).toMatchObject({ token: "--error", label: "Fail" });
    expect(outcomeVisual("skipped")).toMatchObject({ token: "--agent-idle", label: "Skipped" });
  });
});

// ---- the approval threshold, one place both surfaces read ------------------

describe("medium-or-worse", () => {
  it("is medium, high and blocked — never pass or low", () => {
    expect(isMediumOrWorse("pass")).toBe(false);
    expect(isMediumOrWorse("low")).toBe(false);
    expect(isMediumOrWorse("medium")).toBe(true);
    expect(isMediumOrWorse("high")).toBe(true);
    // Blocked sorts as the most severe: a validation that could not judge is
    // worse than one that ran and failed, so it too awaits a human.
    expect(isMediumOrWorse("blocked")).toBe(true);
  });

  it("awaitingApproval is medium-or-worse AND not yet approved", () => {
    expect(awaitingApproval(result("a", { risk: "medium" }))).toBe(true);
    expect(awaitingApproval(result("a", { risk: "pass" }))).toBe(false);
    expect(
      awaitingApproval(
        result("a", {
          risk: "high",
          approval: { approver: "you", timestamp: "2026-08-08T10:00:02Z", note: null },
        }),
      ),
    ).toBe(false);
  });

  it("reviewCount counts only the subjects still awaiting review (hides at zero)", () => {
    expect(reviewCount([])).toBe(0);
    expect(
      reviewCount([
        result("a", { risk: "medium" }),
        result("b", { risk: "pass" }),
        result("c", { risk: "blocked" }),
      ]),
    ).toBe(2);
  });
});

// ---- the held map ordering + the Mission Control join ----------------------

describe("orderResults / latestForSession", () => {
  it("orders results oldest-first by created_at, then id", () => {
    const a = result("a", { created_at: "2026-08-08T09:00:00Z" });
    const b = result("b", { created_at: "2026-08-08T11:00:00Z" });
    expect(orderResults([b, a]).map((r) => r.validation_id)).toEqual(["a", "b"]);
  });

  it("latestForSession takes the newest session_output result, ignoring file subjects", () => {
    const older = result("old", { created_at: "2026-08-08T09:00:00Z" });
    const newer = result("new", { created_at: "2026-08-08T12:00:00Z" });
    const file = result("file", {
      created_at: "2026-08-08T13:00:00Z",
      subject: { kind: "file", ref: "sess-1", label: "a file" },
    });
    expect(latestForSession([older, file, newer], "sess-1")?.validation_id).toBe("new");
    expect(latestForSession([file], "sess-1")).toBeNull();
    expect(latestForSession([], "sess-1")).toBeNull();
  });
});

describe("buildCards fifth read (the Mission Control join)", () => {
  const activity = {
    session_id: "sess-1",
    folder: "",
    title: "s1",
    kind: "chat" as const,
    entries: [],
    dropped: 0,
    active_at: 0,
  };
  const inputs = {
    sessions: [activity],
    orchestrators: null,
    costs: [],
    slots: [],
    permissions: [],
    states: {},
  };

  it("attaches the latest validation for a session's output to its card", () => {
    const val = result("v1", { risk: "high" });
    const [card] = buildCards({ ...inputs, validations: [val] });
    expect(card?.validation?.validation_id).toBe("v1");
    // No new number: it is the same object, not a copy or a re-derivation.
    expect(card?.validation).toBe(val);
  });

  it("is null when the session has no validation, and when none is supplied", () => {
    expect(buildCards({ ...inputs, validations: [] })[0]?.validation).toBeNull();
    expect(buildCards(inputs)[0]?.validation).toBeNull();
  });
});

// ---- the store: run + approve, with a mocked REST client -------------------

describe("useValidationStore.approve", () => {
  it("posts the decision and reflects the recorded approval in the map", async () => {
    const awaiting = result("v1", { risk: "medium" });
    const approved = result("v1", {
      risk: "medium",
      approval: { approver: "you", timestamp: "2026-08-08T10:00:05Z", note: "ok" },
    });
    approveValidation.mockResolvedValue(approved);
    useValidationStore.setState({ results: { v1: awaiting } });

    const returned = await useValidationStore.getState().approve("v1", "you", "ok");

    expect(approveValidation).toHaveBeenCalledWith("v1", { approver: "you", note: "ok" });
    expect(returned.approval?.approver).toBe("you");
    expect(useValidationStore.getState().results.v1?.approval?.note).toBe("ok");
  });

  it("a 404 throws the ApiError and drops the stale entry from the map", async () => {
    approveValidation.mockRejectedValue(new ApiError(404, "unknown or superseded validation"));
    useValidationStore.setState({ results: { v1: result("v1", { risk: "high" }) } });

    await expect(useValidationStore.getState().approve("v1", "you", null)).rejects.toBeInstanceOf(
      ApiError,
    );
    // The dead result is gone, so the pane falls through to its tombstone.
    expect(useValidationStore.getState().results.v1).toBeUndefined();
  });

  it("run stores the returned result under its id", async () => {
    const created = result("v9", { risk: "blocked" });
    runValidation.mockResolvedValue(created);
    await useValidationStore.getState().run({
      subject: { kind: "session_output", ref: "sess-1", label: "s1" },
    });
    expect(useValidationStore.getState().results.v9?.risk).toBe("blocked");
  });
});
