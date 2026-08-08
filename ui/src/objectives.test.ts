/**
 * Objectives' pure rules and the goal-text store.
 *
 * The status derivation is the promise that makes the badge honest — it must
 * agree with the server's own table (`services/sessions.py`) — so it is
 * unit-tested rather than trusted, across every status and the superseded/evicted
 * edges. The store's set/load/clear are tested against a mocked REST client; two
 * sessions are shown to carry two independent goals (the plural obligation).
 *
 * The browser half — the panel rendering, the E2E journey that flips a real
 * objective to `met` — is `ObjectivePanel.test.tsx` and `ui/e2e/objective.spec.ts`.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const { clearObjective, getObjective, getWorkspace, listNamedSessions, setObjective } = vi.hoisted(
  () => ({
    clearObjective: vi.fn(),
    getObjective: vi.fn(),
    getWorkspace: vi.fn(),
    listNamedSessions: vi.fn(),
    setObjective: vi.fn(),
  }),
);
vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof import("./api")>("./api");
  return { ...actual, clearObjective, getObjective, getWorkspace, listNamedSessions, setObjective };
});

import type { NamedSession, ObjectiveStatus, RiskLevel, ValidationResult } from "./types";
import {
  goalFor,
  latestForObjective,
  objectiveStatus,
  objectiveStatusVisual,
  resetObjectiveStoreForTests,
  useObjectiveStore,
} from "./objectives";

function result(
  ref: string,
  risk: RiskLevel,
  opts: { id: string; approved?: boolean; kind?: string; at?: string } = { id: "v" },
): ValidationResult {
  return {
    validation_id: opts.id,
    subject: { kind: (opts.kind ?? "objective") as "objective", ref, label: ref },
    risk,
    evidence: [],
    summary: `${risk} for ${ref}`,
    created_at: opts.at ?? "2026-08-08T12:00:00Z",
    completed_at: opts.at ?? "2026-08-08T12:00:00Z",
    truncated: null,
    approval: opts.approved === true ? { approver: "you", timestamp: opts.at ?? "x", note: null } : null,
  };
}

const GOAL = { statement: "reconcile", acceptance: null };

afterEach(() => {
  resetObjectiveStoreForTests();
  vi.clearAllMocks();
});

// ---- the derivation, in lockstep with the server ----------------------------

describe("objectiveStatus", () => {
  it("is open with no objective or no evidence", () => {
    expect(objectiveStatus(null, null)).toBe("open");
    expect(objectiveStatus(GOAL, null)).toBe("open");
  });

  it("is met only when the latest result is approved — approval is the gate", () => {
    expect(objectiveStatus(GOAL, result("s", "pass", { id: "v", approved: true }))).toBe("met");
    // A clean pass nobody signed off is still open.
    expect(objectiveStatus(GOAL, result("s", "pass", { id: "v" }))).toBe("open");
  });

  it("reads at-risk / failing off an unapproved medium-or-worse latest", () => {
    expect(objectiveStatus(GOAL, result("s", "medium", { id: "v" }))).toBe("at-risk");
    expect(objectiveStatus(GOAL, result("s", "high", { id: "v" }))).toBe("failing");
    expect(objectiveStatus(GOAL, result("s", "blocked", { id: "v" }))).toBe("failing");
    // A human override closes even a risky result.
    expect(objectiveStatus(GOAL, result("s", "high", { id: "v", approved: true }))).toBe("met");
  });
});

describe("latestForObjective", () => {
  it("takes the newest objective-subject result for the session, ignoring other kinds", () => {
    const results = [
      result("sess", "pass", { id: "old", approved: true, at: "2026-08-08T12:00:00Z" }),
      result("sess", "medium", { id: "new", at: "2026-08-08T12:05:00Z" }),
      // Same ref, different kind — must not be mistaken for the objective's.
      result("sess", "high", { id: "out", kind: "session_output", at: "2026-08-08T12:09:00Z" }),
      // Another session's objective — must not leak.
      result("other", "high", { id: "elsewhere", at: "2026-08-08T12:20:00Z" }),
    ];
    const latest = latestForObjective(results, "sess");
    expect(latest?.validation_id).toBe("new");
    // So the superseded approved result no longer keeps it met.
    expect(objectiveStatus(GOAL, latest)).toBe("at-risk");
  });

  it("is null when nothing has validated the objective", () => {
    expect(latestForObjective([], "sess")).toBeNull();
  });
});

describe("goalFor", () => {
  const session = { id: "a", objective: { statement: "goal A", acceptance: null } };

  it("uses the manifest snapshot only while the map has no entry (undefined = not loaded)", () => {
    expect(goalFor({}, session)?.statement).toBe("goal A");
    expect(goalFor({}, { id: "b" })).toBeNull();
  });

  it("prefers the loaded map value over the manifest snapshot", () => {
    expect(goalFor({ a: { statement: "fresh goal", acceptance: null } }, session)?.statement).toBe(
      "fresh goal",
    );
  });

  it("honours an explicit cleared entry (null) instead of falling back to a stale manifest", () => {
    // The bug: clearing an objective sets objectives[id] = null but leaves the
    // session list snapshot untouched. A `??` chain would fall through null back
    // to session.objective; `goalFor` must read the clear.
    expect(goalFor({ a: null }, session)).toBeNull();
  });
});

describe("objectiveStatusVisual", () => {
  it("maps each status onto an existing semantic token — no new colour", () => {
    const cases: Record<ObjectiveStatus, string> = {
      open: "--agent-idle",
      met: "--success",
      "at-risk": "--warn",
      failing: "--error",
    };
    for (const [status, token] of Object.entries(cases)) {
      expect(objectiveStatusVisual(status as ObjectiveStatus).token).toBe(token);
      // Every visual carries a word — colour is never the only signal.
      expect(objectiveStatusVisual(status as ObjectiveStatus).label).not.toBe("");
    }
  });
});

// ---- the store: two sessions, two independent goals -------------------------

function namedSession(id: string, name: string, objective: NamedSession["objective"] = null): NamedSession {
  return {
    id,
    name,
    workspace: "C:/proj",
    arrangement: null,
    agents: [],
    leases: [],
    created_at: 0,
    last_attached_at: 0,
    objective,
  };
}

describe("the objective store", () => {
  it("refreshes the session list and seeds goals from the manifests", async () => {
    getWorkspace.mockResolvedValue({ root: "C:/proj" });
    listNamedSessions.mockResolvedValue({
      sessions: [
        namedSession("a", "Session A", { statement: "goal A", acceptance: null }),
        namedSession("b", "Session B", null),
      ],
      problem: null,
    });

    await useObjectiveStore.getState().refresh();
    const state = useObjectiveStore.getState();
    expect(state.sessions.map((s) => s.id)).toEqual(["a", "b"]);
    expect(state.objectives.a?.statement).toBe("goal A");
    expect(state.objectives.b).toBeNull();
  });

  it("keeps two sessions' goals independent through set and clear", async () => {
    setObjective.mockImplementation((id: string, body: { statement: string }) =>
      Promise.resolve({ session_id: id, objective: { statement: body.statement, acceptance: null }, status: "open", evidence: null }),
    );
    clearObjective.mockResolvedValue({ session_id: "a", objective: null, status: "open", evidence: null });

    await useObjectiveStore.getState().set("a", "goal A", null);
    await useObjectiveStore.getState().set("b", "goal B", null);
    expect(useObjectiveStore.getState().objectives.a?.statement).toBe("goal A");
    expect(useObjectiveStore.getState().objectives.b?.statement).toBe("goal B");

    // Clearing A leaves B untouched — no singleton "active objective".
    await useObjectiveStore.getState().clear("a");
    expect(useObjectiveStore.getState().objectives.a).toBeNull();
    expect(useObjectiveStore.getState().objectives.b?.statement).toBe("goal B");
  });

  it("loads one session's objective by id", async () => {
    getObjective.mockResolvedValue({
      session_id: "a",
      objective: { statement: "loaded goal", acceptance: "0.1%" },
      status: "open",
      evidence: null,
    });
    await useObjectiveStore.getState().load("a");
    expect(useObjectiveStore.getState().objectives.a?.acceptance).toBe("0.1%");
  });
});
