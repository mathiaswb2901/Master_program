/**
 * The join, and the rules that make the board honest.
 *
 * Pure functions over the rows the server sends — which is the whole of what
 * this capability computes, because everything else it shows is *composed*
 * rather than derived (ROADMAP M5 item 7's re-scope: one live-fleet view, not
 * two that can disagree).
 */

import { describe, expect, it, vi } from "vitest";

// Answering a prompt from the board settles the *shared* record every surface
// renders (`store.permissions`), so this module reaches the app store — which
// reads `document` at import, and the suite is node-only. None of the pure
// folds below touch it.
vi.mock("./store", () => ({
  useStore: { getState: () => ({ settlePermission: () => undefined }) },
}));

import {
  buildCards,
  crewOf,
  formatSpend,
  mergePermissions,
  mergeSlots,
  orderCards,
  pendingCount,
  refusalHint,
  refusalRatio,
  slotFor,
  waitingFor,
  workersById,
  type MissionCard,
} from "./mission";
import type {
  OrchestratorSnapshot,
  SessionActivity,
  SessionPermissions,
  SpawnRefusal,
  WorkerInfo,
  WorktreeInfo,
} from "./types";

const T = 1_700_000_000;

function activity(id: string, patch: Partial<SessionActivity> = {}): SessionActivity {
  return {
    session_id: id,
    folder: "",
    title: `session ${id}`,
    kind: "chat",
    entries: [],
    dropped: 0,
    active_at: T,
    ...patch,
  };
}

function worker(id: string, patch: Partial<WorkerInfo> = {}): WorkerInfo {
  return {
    worker_id: id,
    orchestrator_id: "boss",
    task: "do the thing",
    slot: "slot-01",
    path: "C:/pool/slot-01",
    turns: 0,
    outcome: null,
    detail: null,
    created_at: T,
    updated_at: T,
    ...patch,
  };
}

function slot(name: string, patch: Partial<WorktreeInfo> = {}): WorktreeInfo {
  return {
    slot: name,
    path: `C:/pool/${name}`,
    state: "leased",
    head: "abc1234",
    lease: null,
    dirty_files: 0,
    created_at: T,
    updated_at: T,
    detail: null,
    ...patch,
  };
}

function roster(workers: WorkerInfo[]): OrchestratorSnapshot {
  return {
    orchestrators: [{ orchestrator_id: "boss", workers, last_refusal: null }],
    budget: {
      max_workers: 4,
      max_worker_turns: 40,
      max_worker_cost_usd: 5,
      max_fleet_turns: 200,
      max_fleet_cost_usd: 20,
    },
    max_concurrent_sessions: 8,
    worktree_capacity: 4,
  };
}

function blocked(id: string, requestId = "r1"): SessionPermissions {
  return {
    session_id: id,
    title: `session ${id}`,
    folder: "",
    kind: "worker",
    pending: [
      { request_id: requestId, tool: "Bash", description: "Bash: ls", requested_at: T },
    ],
  };
}

function cards(input: Partial<Parameters<typeof buildCards>[0]> = {}): MissionCard[] {
  return buildCards({
    sessions: [],
    orchestrators: null,
    costs: [],
    slots: [],
    permissions: [],
    states: {},
    ...input,
  });
}

// ---- the join -----------------------------------------------------------------

describe("building a card", () => {
  it("joins four services into one row and derives none of them", () => {
    const [card] = cards({
      sessions: [activity("w1", { title: "fix the parser", folder: "server" })],
      orchestrators: roster([worker("w1")]),
      costs: [{ session_id: "w1", turns: 3, cost_usd: 1.25, observed_at: T }],
      slots: [slot("slot-01")],
      permissions: [blocked("w1")],
      states: { w1: "needs_attention" },
    });
    expect(card?.title).toBe("fix the parser");
    expect(card?.kind).toBe("worker");
    expect(card?.state).toBe("needs_attention");
    expect(card?.cost?.cost_usd).toBe(1.25);
    expect(card?.slot?.slot).toBe("slot-01");
    expect(card?.pending).toHaveLength(1);
  });

  it("lists a session the activity feed knows even before it has run anything", () => {
    // The feed is the fleet: a session appears there when it is created, and a
    // board that waited for a tool call would show an empty window for the
    // first seconds of every session.
    expect(cards({ sessions: [activity("a")] })).toHaveLength(1);
  });

  it("reads an orchestrator's kind from the activity feed, before it spawns", () => {
    // The whole point of carrying `kind` on the fleet's per-session identity: a
    // freshly-started orchestrator has no worker roster and no pending prompt,
    // so the board would otherwise read it as an ordinary chat until it acted.
    const [card] = cards({ sessions: [activity("boss", { kind: "orchestrator" })] });
    expect(card?.kind).toBe("orchestrator");
  });

  it("keeps a stopped worker's card while its orchestrator still lists it", () => {
    // "That one failed and cost $1.20" is the fact the next spawn decision is
    // made on. Dropping the row when the process ends takes it away exactly
    // when it becomes useful.
    const [card] = cards({
      sessions: [],
      orchestrators: roster([worker("w1", { outcome: "failed", detail: "fell over" })]),
      costs: [{ session_id: "w1", turns: 2, cost_usd: 1.2, observed_at: T }],
    });
    expect(card?.worker?.outcome).toBe("failed");
    expect(card?.cost?.cost_usd).toBe(1.2);
    // With no activity row left, its title falls back to the task it was given.
    expect(card?.title).toBe("do the thing");
  });

  it("reads a call in flight as working when the window never listed the session", () => {
    const running = activity("a", {
      entries: [
        {
          entry_id: "e1",
          tool: "Read",
          summary: "Read: x.py",
          target: "x.py",
          started_at: T,
          settled_at: null,
          ok: null,
        },
      ],
    });
    expect(cards({ sessions: [running] })[0]?.state).toBe("working");
    expect(cards({ sessions: [activity("a")] })[0]?.state).toBe("idle");
  });

  it("takes the server's state over the inferred one when it has heard it", () => {
    expect(cards({ sessions: [activity("a")], states: { a: "needs_attention" } })[0]?.state).toBe(
      "needs_attention",
    );
  });
});

// ---- order --------------------------------------------------------------------

describe("board order", () => {
  it("puts anything waiting on a human first, ahead of the busiest session", () => {
    const busy = activity("busy", { active_at: T + 1000 });
    const stuck = activity("stuck", { active_at: T - 1000 });
    const order = cards({
      sessions: [busy, stuck],
      permissions: [blocked("stuck")],
      states: { busy: "working", stuck: "idle" },
    }).map((card) => card.sessionId);
    expect(order).toEqual(["stuck", "busy"]);
  });

  it("then needs_attention, then working, then quiet", () => {
    const rows = orderCards([
      { ...base("idle"), sessionId: "d", state: "idle" },
      { ...base("working"), sessionId: "c", state: "working" },
      { ...base("attention"), sessionId: "b", state: "needs_attention" },
      { ...base("blocked"), sessionId: "a", state: "idle", pending: blocked("a").pending },
    ]);
    expect(rows.map((row) => row.sessionId)).toEqual(["a", "b", "c", "d"]);
  });

  it("breaks ties on id, so equal rows do not reshuffle every frame", () => {
    const rows = orderCards([
      { ...base("x"), sessionId: "z" },
      { ...base("y"), sessionId: "a" },
    ]);
    expect(rows.map((row) => row.sessionId)).toEqual(["a", "z"]);
  });
});

function base(id: string): MissionCard {
  return {
    sessionId: id,
    title: id,
    folder: "",
    kind: "chat",
    state: "idle",
    activity: null,
    cost: null,
    worker: null,
    slot: null,
    pending: [],
  };
}

// ---- slots --------------------------------------------------------------------

describe("matching a worker to its slot", () => {
  it("matches on name *and* path", () => {
    expect(slotFor(worker("w1"), [slot("slot-01")])?.slot).toBe("slot-01");
  });

  it("refuses a slot of the same name at a different path", () => {
    // The failure this guards: a slot released and re-leased to somebody else
    // keeps its name, and matching by name alone would show another agent's
    // lease on this card.
    expect(slotFor(worker("w1"), [slot("slot-01", { path: "C:/other/slot-01" })])).toBeNull();
  });

  it("is null for a session that holds no slot at all", () => {
    expect(slotFor(null, [slot("slot-01")])).toBeNull();
    expect(slotFor(worker("w1", { slot: null }), [slot("slot-01")])).toBeNull();
  });

  it("appends a slot the pool has only just created", () => {
    // The `worktree_changed` frame for a brand-new slot arrives before any
    // fetch has listed it; dropping it would leave the card blank.
    expect(mergeSlots([], slot("slot-02")).map((s) => s.slot)).toEqual(["slot-02"]);
    expect(
      mergeSlots([slot("slot-01")], slot("slot-01", { state: "free" }))[0]?.state,
    ).toBe("free");
  });
});

// ---- permissions --------------------------------------------------------------

describe("the permission channel", () => {
  it("replaces one session's whole set, and an empty set removes the row", () => {
    // A delta would leave a card standing for a future that is already
    // resolved — and the user would "allow" something the agent gave up on.
    const one = mergePermissions([], blocked("a"));
    expect(one).toHaveLength(1);
    expect(mergePermissions(one, { ...blocked("a"), pending: [] })).toEqual([]);
  });

  it("leaves other sessions alone", () => {
    const both = mergePermissions(mergePermissions([], blocked("a")), blocked("b"));
    expect(mergePermissions(both, { ...blocked("a"), pending: [] }).map((r) => r.session_id)).toEqual(
      ["b"],
    );
  });

  it("counts prompts, not sessions — two prompts is two decisions", () => {
    const two: SessionPermissions = {
      ...blocked("a"),
      pending: [...blocked("a").pending, ...blocked("a", "r2").pending],
    };
    expect(pendingCount(cards({ sessions: [activity("a")], permissions: [two] }))).toBe(2);
  });
});

// ---- what a refusal has to say -----------------------------------------------

describe("a ceiling that bound", () => {
  const refusal: SpawnRefusal = {
    reason: "worker_limit",
    detail: "3 of 3 workers are already running",
    limit: 3,
    observed: 3,
    setting: "WORKBENCH_ORCHESTRATOR_MAX_WORKERS",
  };

  it("names the setting that lifts it — never a dead button", () => {
    expect(refusalHint(refusal)).toBe("Raise WORKBENCH_ORCHESTRATOR_MAX_WORKERS to lift this ceiling.");
  });

  it("has no hint when no setting raises it", () => {
    expect(refusalHint({ ...refusal, setting: null })).toBeNull();
  });

  it("renders a ratio only when both halves are known", () => {
    expect(refusalRatio(refusal)).toBe("3 of 3");
    expect(refusalRatio({ ...refusal, observed: null })).toBeNull();
    expect(refusalRatio({ ...refusal, limit: 12.5, observed: 4 })).toBe("4 of 12.50");
  });
});

// ---- small readings -----------------------------------------------------------

describe("readings", () => {
  it("does not round sub-cent spend away — short turns really cost that", () => {
    expect(formatSpend(0)).toBe("$0");
    expect(formatSpend(0.0042)).toBe("$0.0042");
    expect(formatSpend(1.5)).toBe("$1.50");
  });

  it("counts a prompt's wait in the words the card uses", () => {
    expect(waitingFor(T, T + 12)).toBe("12s");
    expect(waitingFor(T, T + 125)).toBe("2m 5s");
    // A clock that disagrees never produces a negative wait.
    expect(waitingFor(T, T - 5)).toBe("0s");
  });

  it("indexes every worker of every orchestrator", () => {
    expect([...workersById(roster([worker("a"), worker("b")])).keys()]).toEqual(["a", "b"]);
    expect(workersById(null).size).toBe(0);
  });

  it("scopes a crew to its own orchestrator", () => {
    const fleet = cards({
      orchestrators: {
        ...roster([worker("a")]),
        orchestrators: [
          { orchestrator_id: "boss", workers: [worker("a")], last_refusal: null },
          {
            orchestrator_id: "other",
            workers: [worker("b", { orchestrator_id: "other" })],
            last_refusal: null,
          },
        ],
      },
    });
    expect(crewOf(fleet, "boss").map((card) => card.sessionId)).toEqual(["a"]);
    expect(crewOf(fleet, "other").map((card) => card.sessionId)).toEqual(["b"]);
  });
});
