/**
 * The board at zero, one and four workers — and with a permission waiting.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design —
 * `vitest.config.ts`). Four workers with one of them blocked is the state the
 * whole feature is judged on and the one a browser journey cannot reliably
 * stage — four live agents at four different points of their turns at the same
 * instant — so it is asserted here, on the rendering, and `ui/e2e/mission.spec.ts`
 * covers what a real crew can reach.
 *
 * The join is tested next to it (`mission.test.ts` is the pure half): what these
 * assertions are really about is that the board renders the *services'* rows and
 * derives nothing — a card's cost comes from the usage snapshot, its activity
 * from the activity feed, its slot from the pool, and its prompts from the
 * fleet-wide permission channel.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { orderCards, type MissionCard } from "../mission";
import type {
  OrchestratorSnapshot,
  PendingPermission,
  SessionActivity,
  SessionKind,
  SessionState,
  SpawnRefusal,
  WorkerInfo,
} from "../types";

import { EMPTY_STATE_ICON, MissionBody, MissionCardView, MissionReading } from "./MissionControl";

// The neighbours this panel reaches through and never exercises here.
// `openPanel` and `revealPane` pull in the whole registry (and with it Monaco's
// browser bundle); the store reads `document` at import; `statusVisual` — the
// §2.6 vocabulary this board deliberately shares with the session rows rather
// than restating — lives next to the plan card, which builds a draft off the
// real store at module scope. The mapping under test is the real one.
vi.mock("../dock", () => ({ openPanel: () => undefined }));
vi.mock("./PlanCard", () => ({ PlanCard: () => null }));
vi.mock("./Panes", () => ({ revealPane: () => undefined }));
vi.mock("../store", () => ({
  useStore: Object.assign((select: (state: unknown) => unknown) => select({ sessionFlags: {} }), {
    getState: () => ({}),
  }),
}));
vi.mock("../mission", async (importOriginal) => {
  // Only the store is stubbed; every pure derivation is the shipped one.
  const actual = await importOriginal<typeof import("../mission")>();
  return {
    ...actual,
    useMissionStore: Object.assign(
      (select: (state: unknown) => unknown) =>
        select({ answer: () => undefined, stop: () => undefined, slots: [] }),
      { getState: () => ({ init: () => undefined }) },
    ),
  };
});

const T = 1_700_000_000;
const NOW_MS = T * 1000;

function activity(id: string, summary: string, running = true): SessionActivity {
  return {
    session_id: id,
    folder: "",
    title: `session ${id}`,
    kind: "chat",
    entries: [
      {
        entry_id: `${id}-1`,
        tool: "Edit",
        summary,
        target: null,
        started_at: T,
        settled_at: running ? null : T,
        ok: running ? null : true,
      },
    ],
    dropped: 0,
    active_at: T,
  };
}

function worker(id: string, patch: Partial<WorkerInfo> = {}): WorkerInfo {
  return {
    worker_id: id,
    orchestrator_id: "boss",
    task: `task for ${id}`,
    slot: `slot-0${id.slice(-1)}`,
    path: `C:/pool/slot-0${id.slice(-1)}`,
    turns: 1,
    outcome: null,
    detail: null,
    created_at: T,
    updated_at: T,
    ...patch,
  };
}

function prompt(patch: Partial<PendingPermission> = {}): PendingPermission {
  return {
    request_id: "req-1",
    tool: "Bash",
    description: "Bash: rm -rf build",
    requested_at: T - 45,
    ...patch,
  };
}

function card(id: string, patch: Partial<MissionCard> = {}): MissionCard {
  return {
    sessionId: id,
    title: `session ${id}`,
    folder: "",
    kind: "chat" as SessionKind,
    state: "working" as SessionState,
    activity: activity(id, `Edit: src/${id}.py`),
    cost: { session_id: id, turns: 2, cost_usd: 0.42, observed_at: T },
    worker: null,
    slot: null,
    pending: [],
    ...patch,
  };
}

function orchestrators(patch: Partial<OrchestratorSnapshot> = {}): OrchestratorSnapshot {
  return {
    orchestrators: [{ orchestrator_id: "boss", workers: [], last_refusal: null }],
    budget: {
      max_workers: 4,
      max_worker_turns: 40,
      max_worker_cost_usd: 5,
      max_fleet_turns: 200,
      max_fleet_cost_usd: 20,
    },
    max_concurrent_sessions: 8,
    worktree_capacity: 4,
    ...patch,
  };
}

function board(cards: MissionCard[], snapshot: OrchestratorSnapshot | null = null): string {
  return renderToStaticMarkup(
    <MissionBody cards={cards} orchestrators={snapshot} now={NOW_MS} />,
  );
}

// ---- zero ---------------------------------------------------------------------

describe("an idle fleet", () => {
  it("is a designed state, not an empty panel", () => {
    const html = board([]);
    expect(html).toContain("mission-empty");
    expect(html).toContain("No agents running");
    // It says what the board is *for*, including the half nothing else offers.
    expect(html).toContain("never opened");
    expect(html).toContain("No sessions");
  });

  it("draws its icon at the size DESIGN.md §6.10 fixes", () => {
    expect(EMPTY_STATE_ICON).toEqual({ size: 32, strokeWidth: 1.5 });
    const html = board([]);
    expect(html).toContain(`width="32"`);
    expect(html).toContain(`stroke-width="1.5"`);
  });

  it("shows nothing in the status bar when nothing is waiting", () => {
    // §6.7: counts hide at zero — and this is deliberately not another
    // "sessions working" count, of which the bar already has two.
    expect(renderToStaticMarkup(<MissionReading cards={[]} orchestrators={null} now={NOW_MS} />)).toBe(
      "",
    );
    const busy = [card("a"), card("b")];
    expect(
      renderToStaticMarkup(<MissionReading cards={busy} orchestrators={null} now={NOW_MS} />),
    ).toBe("");
  });
});

// ---- one ----------------------------------------------------------------------

describe("one session", () => {
  it("renders what it is doing, what it cost and where it is working", () => {
    const html = board([
      card("a", {
        kind: "worker",
        worker: worker("a"),
        slot: {
          slot: "slot-01",
          path: "C:/pool/slot-01",
          state: "leased",
          head: "abc",
          lease: null,
          dirty_files: 0,
          created_at: T,
          updated_at: T,
          detail: null,
        },
      }),
    ]);
    expect(html).toContain("Edit: src/a.py"); // from the activity feed
    expect(html).toContain("$0.42"); // from the usage service
    expect(html).toContain("slot-01"); // from the worktree pool
    expect(html).toContain("Worker"); // from the orchestrator roster
    expect(html).toContain("1 of 1 working");
  });

  it("says an em dash rather than zero for a session that has not finished a turn", () => {
    // Absent and measured-zero are different statements, and a board that
    // rendered "$0.00" for a session that has spent nothing *we know of* would
    // be claiming knowledge it does not have.
    const html = board([card("a", { cost: null })]);
    expect(html).toContain("—");
    expect(html).not.toContain("$0.00");
  });

  it("names a stopped worker's outcome instead of pretending it is quiet", () => {
    const html = board([
      card("a", {
        kind: "worker",
        state: "idle",
        activity: null,
        worker: worker("a", { outcome: "failed", detail: "the worker fell over" }),
      }),
    ]);
    expect(html).toContain("Failed");
    expect(html).toContain("the worker fell over");
  });
});

// ---- four, one of them blocked ------------------------------------------------

describe("four workers", () => {
  const crew = [
    card("w1", { kind: "worker", worker: worker("w1") }),
    card("w2", { kind: "worker", worker: worker("w2") }),
    card("w3", { kind: "worker", worker: worker("w3"), state: "idle" }),
    card("w4", {
      kind: "worker",
      worker: worker("w4"),
      state: "needs_attention",
      pending: [prompt()],
    }),
  ];

  it("stays readable, and every worker has its own card", () => {
    const html = board(crew, orchestrators());
    for (const id of ["w1", "w2", "w3", "w4"]) {
      expect(html).toContain(`data-session="${id}"`);
    }
    expect(html).toContain("2 of 4 working");
    expect(html).toContain("1 waiting on you");
  });

  it("puts the blocked worker first, whatever the activity clock says", () => {
    // Not recency: a card that scrolled away because a chattier session moved
    // above it is the failure this board exists to fix. The fixture is
    // deliberately in the *wrong* order, and `orderCards` — the same function
    // the panel's own `useMission` runs — is what puts it right, so this
    // asserts the shipped path rather than a pre-sorted array.
    const html = board(orderCards(crew), orchestrators());
    const at = (id: string): number => html.indexOf(`data-session="${id}"`);
    expect(at("w4")).toBeGreaterThan(0);
    expect(at("w4")).toBeLessThan(at("w1"));
    expect(at("w4")).toBeLessThan(at("w3"));
    // …and the quiet one sinks below the two that are working.
    expect(at("w3")).toBeGreaterThan(at("w1"));
  });

  it("answers the permission inline, with the command on screen", () => {
    const html = board(crew, orchestrators());
    expect(html).toContain("Allow");
    expect(html).toContain("Deny");
    // The description is not redacted: an approval you cannot read is one you
    // cannot give informedly.
    expect(html).toContain("rm -rf build");
    expect(html).toContain("Bash");
    // And how long it has waited — the server denies at ten minutes.
    expect(html).toContain("45s");
    expect(html).toContain("is-blocked");
  });

  it("reads the waiting count into the status bar", () => {
    const html = renderToStaticMarkup(
      <MissionReading cards={crew} orchestrators={null} now={NOW_MS} />,
    );
    expect(html).toContain("mission-status");
    expect(html).toContain(">1<");
  });

  it("shows the crew's ceilings and how to raise the one that bound", () => {
    const refusal: SpawnRefusal = {
      reason: "worker_limit",
      detail: "4 of 4 workers are already running — stop one, or raise WORKBENCH_ORCHESTRATOR_MAX_WORKERS",
      limit: 4,
      observed: 4,
      setting: "WORKBENCH_ORCHESTRATOR_MAX_WORKERS",
    };
    const html = board(
      crew,
      orchestrators({
        orchestrators: [
          { orchestrator_id: "boss", workers: [worker("w1")], last_refusal: refusal },
        ],
      }),
    );
    expect(html).toContain("At the ceiling");
    expect(html).toContain("4 of 4");
    // Never a dead button: the setting that lifts it is on screen.
    expect(html).toContain("Raise WORKBENCH_ORCHESTRATOR_MAX_WORKERS");
    expect(html).toContain("Stop crew");
    expect(html).toContain("4 worktree slots");
  });

  it("says plainly when there is no pool for a crew to work in", () => {
    const html = board(crew, orchestrators({ worktree_capacity: null }));
    expect(html).toContain("not a git repository");
    expect(html).toContain("nowhere safe to work");
  });
});

// ---- the kind badge -----------------------------------------------------------

describe("the kind badge", () => {
  it("labels a reviewer and gives it its own class, not a worker's", () => {
    // The bug this guards: a reviewer card fell through the old binary ternary
    // and rendered the "Worker" label with an `is-reviewer` class the stylesheet
    // did not define — mislabelled and unstyled at once. It must read "Reviewer".
    const html = board([card("r", { kind: "reviewer" })]);
    expect(html).toContain("is-reviewer");
    expect(html).toContain("Reviewer");
    expect(html).not.toContain("is-worker");
    expect(html).not.toContain("Worker");
  });

  it("wears no badge on an ordinary chat session", () => {
    // `chat` is every session's default; a badge on all of them is noise.
    expect(board([card("c", { kind: "chat" })])).not.toContain("wb-mission-kind");
  });

  it("fails safe to no badge for a kind the union does not know", () => {
    // `card.kind` is a wire value with no runtime validation, so a rolling deploy
    // that leaves this bundle behind the server can hand it a kind outside the
    // compiled-in union. The map lookup is `undefined` there, not `null`, and an
    // unguarded ternary would paint a garbled `title="…a undefined"` half-badge on
    // an `is-<unknown>` class no stylesheet defines. It must draw nothing instead.
    const html = board([card("f", { kind: "future-kind" as SessionKind })]);
    expect(html).not.toContain("wb-mission-kind");
    expect(html).not.toContain("undefined");
    expect(html).not.toContain("is-future-kind");
  });
});

// ---- the card in isolation ----------------------------------------------------

describe("one card", () => {
  it("is memoized, so one worker's tool call does not re-render the fleet", () => {
    // Four sources feed this board and any of them replaces its snapshot
    // object; without the boundary, four working agents is a re-render storm.
    expect(MissionCardView.$$typeof).toBe(Symbol.for("react.memo"));
  });
});
