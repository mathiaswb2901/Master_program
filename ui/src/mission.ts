/**
 * Mission Control on the UI side: the composition, and a store next to it.
 *
 * **This module derives no fleet state of its own.** The board is the view over
 * services that already exist, and that is the whole design (ROADMAP M5 item 7,
 * re-scoped 2026-08-05): what a session is *doing* comes from the activity feed
 * (`activity.ts`), what it has *cost* from the usage service (`usage.ts`), what
 * *slot* it holds from the worktree pool, and what *state* it is in from the app
 * store's `session_status` map. Two live-fleet views that disagree is worse than
 * one, so the only thing computed here is the join.
 *
 * What is genuinely new is the other two channels, because nothing else carries
 * them: an orchestrator's roster with its budget (`/api/orchestrator`), and the
 * fleet-wide **permission** channel — the gap that makes this feature worth
 * building. A blocked agent in a session nobody opened is invisible today until
 * it times out ten minutes later, and once an orchestrator is spawning workers
 * the session that blocks is by definition one the user never opened.
 *
 * Every rule below is a pure function over the rows the server sends, so it is
 * unit-tested rather than trusted (`mission.test.ts`).
 *
 * The store is this capability's own zustand instance rather than a slice of
 * `store.ts` — nothing outside this module and its panel reads it, which is
 * exactly the condition CLAUDE.md puts on a second store (`usage.ts` and
 * `activity.ts` are the shipped precedents). It subscribes to the shared
 * `/ws/events` socket for its own two frame types.
 */

import { create } from "zustand";

import * as api from "./api";
// The one thing this module reads from the app store: a prompt is app-wide
// state (a chat pane renders the same one as a card), so answering it here has
// to move the shared record rather than only this board's copy.
import { useStore } from "./store";
import type {
  OrchestratorSnapshot,
  PendingPermission,
  SessionActivity,
  SessionKind,
  SessionPermissions,
  SessionState,
  SpawnRefusal,
  UsageSessionEntry,
  ValidationResult,
  WorkerInfo,
  WorkspaceEvent,
  WorktreeInfo,
} from "./types";
import { latestForSession } from "./validation";
import { ReconnectingSocket } from "./ws";

// ---- one card's worth of fleet ----------------------------------------------

/** Everything the board knows about one session, joined from four sources. */
export interface MissionCard {
  sessionId: string;
  title: string;
  folder: string;
  kind: SessionKind;
  state: SessionState;
  /** From the activity feed. Null when nothing is in flight. */
  activity: SessionActivity | null;
  /** From the usage service. Null until a turn has settled — not a zero. */
  cost: UsageSessionEntry | null;
  /** Set for a worker: the roster row that names its parent and its slot. */
  worker: WorkerInfo | null;
  /** The pool slot this session holds, when it holds one. */
  slot: WorktreeInfo | null;
  /** Prompts this session is blocked on, answerable from the card. */
  pending: PendingPermission[];
  /** The latest `ValidationResult` for this session's output — the fifth read
   * (M6 PR3). The card renders its risk badge *from this object*, deriving no
   * new number: the card's risk and the Review panel's risk are the same result
   * by construction. Null until this session has been validated.
   *
   * `buildCards` always sets it; it is declared optional only so a card literal
   * built in a test that predates the read (Mission Control's own fixtures)
   * stays valid without every fixture restating `validation: null`. */
  validation?: ValidationResult | null;
}

/** Order: anything blocked on the human first, then the busy, then the quiet.
 *
 * Deliberately *not* pure recency. This board exists to surface an agent that
 * is waiting on you; a card that scrolled out of view because a chattier
 * session moved above it is the failure it was built to fix. Ties break on the
 * activity feed's own ordering, then on id so equal rows do not reshuffle on
 * every frame. */
export function orderCards(cards: readonly MissionCard[]): MissionCard[] {
  const rank = (card: MissionCard): number => {
    if (card.pending.length > 0) return 0;
    if (card.state === "needs_attention") return 1;
    if (card.state === "working") return 2;
    return 3;
  };
  return [...cards].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      (b.activity?.active_at ?? 0) - (a.activity?.active_at ?? 0) ||
      a.sessionId.localeCompare(b.sessionId),
  );
}

/** Which pool slot a session is working in, if any.
 *
 * By path rather than by name: a worker's roster row carries the absolute
 * directory the pool handed it, and the pool's own rows carry the same string.
 * Comparing the *slot name* would have worked too and been one field shorter —
 * but it would silently match a slot that had since been released and re-leased
 * to someone else, and this card would then show another agent's lease. */
export function slotFor(
  worker: WorkerInfo | null,
  slots: readonly WorktreeInfo[],
): WorktreeInfo | null {
  if (worker === null || worker.slot === null) return null;
  return slots.find((slot) => slot.slot === worker.slot && slot.path === worker.path) ?? null;
}

/** Every worker of every orchestrator, by worker id. */
export function workersById(snapshot: OrchestratorSnapshot | null): Map<string, WorkerInfo> {
  const byId = new Map<string, WorkerInfo>();
  for (const orchestrator of snapshot?.orchestrators ?? []) {
    for (const worker of orchestrator.workers) byId.set(worker.worker_id, worker);
  }
  return byId;
}

/**
 * The join. One card per session the *activity feed* knows about, because that
 * feed is the fleet — a session exists there from the moment it is created,
 * including one that has never run a tool.
 *
 * A worker whose session has already been reaped keeps its card while the
 * orchestrator still lists it: "that one failed and cost $1.20" is the fact the
 * next decision is made on, and dropping the row the instant the process ends
 * would take it away exactly when it becomes useful.
 */
export function buildCards(input: {
  sessions: readonly SessionActivity[];
  orchestrators: OrchestratorSnapshot | null;
  costs: readonly UsageSessionEntry[];
  slots: readonly WorktreeInfo[];
  permissions: readonly SessionPermissions[];
  states: Readonly<Record<string, SessionState>>;
  /** The fifth read (M6 PR3): every held `ValidationResult`. The card takes the
   * latest one for its session's output and renders its badge — no new number.
   * Optional and defaulting to empty, so a caller that does not yet wire the
   * validation store still builds the same cards with `validation: null`. */
  validations?: readonly ValidationResult[];
}): MissionCard[] {
  const workers = workersById(input.orchestrators);
  const costById = new Map(input.costs.map((entry) => [entry.session_id, entry]));
  const promptsById = new Map(input.permissions.map((row) => [row.session_id, row]));
  const activityById = new Map(input.sessions.map((row) => [row.session_id, row]));
  const validations = input.validations ?? [];

  const ids = new Set<string>([...activityById.keys(), ...workers.keys()]);
  const cards: MissionCard[] = [];
  for (const sessionId of ids) {
    const activity = activityById.get(sessionId) ?? null;
    const worker = workers.get(sessionId) ?? null;
    const prompts = promptsById.get(sessionId);
    // A worker's kind is known from the roster even when the activity feed has
    // no row for it yet; otherwise the activity feed carries it — that feed is
    // the fleet's per-session identity from the moment a session is created, so
    // an orchestrator reads as one before it has spawned a worker or raised a
    // prompt. The prompt frame is the fallback for a card this window heard of
    // through a prompt before the feed, and "an ordinary chat" is the default.
    const kind: SessionKind =
      worker !== null ? "worker" : (activity?.kind ?? prompts?.kind ?? "chat");
    const running = activity?.entries.some((entry) => entry.settled_at === null) === true;
    cards.push({
      sessionId,
      title: activity?.title ?? prompts?.title ?? workerTitle(worker),
      folder: activity?.folder ?? prompts?.folder ?? (worker?.slot ?? ""),
      kind,
      // The server's own state, when the window has heard it. A call in flight
      // is a session working, which is the honest fallback for a card whose
      // session this window has never listed.
      state: input.states[sessionId] ?? (running ? "working" : "idle"),
      activity,
      cost: costById.get(sessionId) ?? null,
      worker,
      slot: slotFor(worker, input.slots),
      pending: prompts?.pending ?? [],
      // The join, not a new authority: the card's risk *is* the latest result's,
      // so a card and its Review pane can never show two computations of "how
      // risky" (M6 PR3, plan §1 "the same result object by construction").
      validation: latestForSession(validations, sessionId),
    });
  }
  return orderCards(cards);
}

function workerTitle(worker: WorkerInfo | null): string {
  return worker === null ? "New session" : worker.task.slice(0, 60);
}

/** Cards belonging to one orchestrator, in board order. */
export function crewOf(cards: readonly MissionCard[], orchestratorId: string): MissionCard[] {
  return cards.filter((card) => card.worker?.orchestrator_id === orchestratorId);
}

/** Prompts across the whole fleet — the status-bar reading and the attention
 * badge. Counts prompts, not sessions: two prompts is two decisions. */
export function pendingCount(cards: readonly MissionCard[]): number {
  return cards.reduce((total, card) => total + card.pending.length, 0);
}

// ---- what a refusal has to say ----------------------------------------------

/** How a cap reads on screen: the ceiling, and the way out.
 *
 * A hit cap that offers nothing is the dead button both `CLAUDE.md` and
 * DESIGN.md forbid, so the *setting* is part of the rendering rather than
 * something a user has to go and find. */
export function refusalHint(refusal: SpawnRefusal): string | null {
  return refusal.setting === null ? null : `Raise ${refusal.setting} to lift this ceiling.`;
}

/** `3 of 4` — only when both numbers are known. A limit with no observation is
 * not a ratio, and rendering one would invent the missing half. */
export function refusalRatio(refusal: SpawnRefusal): string | null {
  if (refusal.limit === null || refusal.observed === null) return null;
  const round = (value: number): string =>
    Number.isInteger(value) ? String(value) : value.toFixed(2);
  return `${round(refusal.observed)} of ${round(refusal.limit)}`;
}

/** Money, at the precision the figure deserves. Sub-cent spend is real on short
 * turns, so it is not rounded away — but a dollar figure is not shown to six
 * places either. */
export function formatSpend(usd: number): string {
  if (usd === 0) return "$0";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(2)}`;
}

/** How long a prompt has been waiting, in the words the card uses. The server's
 * own timeout is ten minutes, so this is not decoration: a card that has waited
 * that long is about to be denied for you. */
export function waitingFor(requestedAt: number, nowSeconds: number): string {
  const seconds = Math.max(0, Math.round(nowSeconds - requestedAt));
  if (seconds < 60) return `${String(seconds)}s`;
  return `${String(Math.floor(seconds / 60))}m ${String(seconds % 60)}s`;
}

// ---- the store ---------------------------------------------------------------

interface MissionStore {
  /** Null until the first fetch answers — distinct from "no orchestrators". */
  orchestrators: OrchestratorSnapshot | null;
  /** Sessions blocked on the human. Empty is a real answer. */
  permissions: SessionPermissions[];
  /** Pool slots, so a worker's card can say which checkout it holds. Lives
   * here rather than in `store.ts` because this board is the pool's only UI
   * reader today (it shipped backend-only in #46) — the same rule `usage.ts`
   * and `activity.ts` follow. A tool that starts sharing moves to `store.ts`. */
  slots: WorktreeInfo[];
  init: () => void;
  refresh: () => Promise<void>;
  answer: (sessionId: string, requestId: string, allow: boolean) => Promise<void>;
  stop: (orchestratorId: string) => Promise<void>;
}

let started = false;

/** Reset the module-level "already subscribed" latch. Tests only. */
export function resetMissionStoreForTests(): void {
  started = false;
  useMissionStore.setState({ orchestrators: null, permissions: [], slots: [] });
}

/** Replace one slot's row, keeping pool order. A slot the pool has only just
 * created arrives on the event before any fetch has listed it, so an unknown
 * id is an append rather than a dropped frame. */
export function mergeSlots(
  current: readonly WorktreeInfo[],
  changed: WorktreeInfo,
): WorktreeInfo[] {
  const known = current.some((slot) => slot.slot === changed.slot);
  return known
    ? current.map((slot) => (slot.slot === changed.slot ? changed : slot))
    : [...current, changed];
}

/** Fold one permission frame into the rows this window holds.
 *
 * The frame carries the whole set for one session, so this is a replace — and
 * a session whose set is now empty is *removed*, which is how a board learns a
 * prompt was answered in the chat, or timed out, or the session closed. */
export function mergePermissions(
  current: readonly SessionPermissions[],
  changed: SessionPermissions,
): SessionPermissions[] {
  const rest = current.filter((row) => row.session_id !== changed.session_id);
  return changed.pending.length === 0 ? rest : [...rest, changed];
}

export const useMissionStore = create<MissionStore>((set, get) => ({
  orchestrators: null,
  permissions: [],
  slots: [],

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type === "orchestrator") {
          set({ orchestrators: event.snapshot });
        } else if (event.type === "session_permission") {
          set((state) => ({ permissions: mergePermissions(state.permissions, event.session) }));
        } else if (event.type === "worktree_changed") {
          set((state) => ({ slots: mergeSlots(state.slots, event.worktree) }));
        }
      },
      // Every reconnect re-reads. Frames that arrived while the socket was down
      // are gone, and a board that missed a prompt opening is a board showing an
      // idle fleet while an agent waits — the exact failure this closes.
      onOpen: () => {
        void get().refresh();
      },
    });
  },

  refresh: async () => {
    // Settled independently: an orchestrator endpoint that fails must not take
    // the permission chips down with it, and a workspace with no git repository
    // (so no pool) must take neither down. The board is a composition of three
    // services and it degrades one service at a time.
    const [orchestrators, permissions, pool] = await Promise.allSettled([
      api.getOrchestrators(),
      api.getPendingPermissions(),
      api.getWorktrees(),
    ]);
    if (orchestrators.status === "fulfilled") set({ orchestrators: orchestrators.value });
    if (permissions.status === "fulfilled") set({ permissions: permissions.value.sessions });
    if (pool.status === "fulfilled") set({ slots: pool.value.slots });
  },

  answer: async (sessionId, requestId, allow) => {
    // Optimistic: the card goes the moment it is clicked, because the round trip
    // is local and a chip that lingers reads as a click that did not land. The
    // server's own frame is what makes it true, and a refusal (the prompt was
    // already settled elsewhere) is corrected by the refresh below.
    set((state) => ({
      permissions: state.permissions
        .map((row) =>
          row.session_id === sessionId
            ? { ...row, pending: row.pending.filter((p) => p.request_id !== requestId) }
            : row,
        )
        .filter((row) => row.pending.length > 0),
    }));
    // The same prompt may be on screen as a *card* in a chat pane, and that card
    // renders one shared record (`store.permissions`). Settling it here is what
    // makes the two surfaces one decision: without it the card kept its Allow
    // and Deny buttons under a question this click had already answered, and
    // pressing one of them hit a settled prompt (404). Recorded before the POST
    // for the same reason the chip goes first — this is the click landing.
    useStore.getState().settlePermission(requestId, allow ? "allow" : "deny");
    try {
      await api.answerPermission(sessionId, { request_id: requestId, allow });
    } catch {
      await get().refresh();
    }
  },

  stop: async (orchestratorId) => {
    try {
      set({ orchestrators: await api.stopOrchestrator(orchestratorId) });
    } catch {
      await get().refresh();
    }
  },
}));
