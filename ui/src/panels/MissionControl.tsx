/**
 * Mission Control — the whole agent fleet on one board.
 *
 * A capability in one module (plus `mission.ts` and its stylesheet): the panel,
 * the status-bar reading, the command that opens it, and the descriptor that
 * registers all three. It edits no shared file except the one line in
 * `tools.ts`.
 *
 * **This board composes; it does not derive.** What a session is *doing* comes
 * from the live activity feed, what it has *cost* from the usage service, what
 * *slot* it holds from the worktree pool, and what *state* it is in from the
 * `session_status` map every window already tracks. That is the ROADMAP's own
 * re-scope of M5 item 7, and it is the difference between one fleet view and two
 * that can disagree.
 *
 * **What is new is the permission chip.** A permission prompt travels
 * `/ws/agent/{id}` — a socket that exists only for conversations a window has
 * *opened*. So today, a blocked agent in a session you have not opened is
 * invisible until it times out ten minutes later; and once an orchestrator is
 * spawning workers, the session that blocks is by definition one you never
 * opened. The fleet-wide `session_permission` channel and the chips below are
 * the point of the feature, not decoration on it.
 *
 * Three renderings that are decisions rather than styling:
 *
 * 1. **Blocked sorts first.** Not recency — a card that scrolled away because a
 *    chattier session moved above it is the failure this board exists to fix.
 * 2. **A hit ceiling shows the ceiling and the setting that raises it.** Never
 *    a dead button (CLAUDE.md's plural-tool rule, ROADMAP principle 4c).
 * 3. **Idle is a designed state.** An empty fleet says so, and says how to
 *    start one — the alternative reads as a broken panel.
 */

import type { IDockviewPanelProps } from "dockview";
import { memo, useEffect, useState } from "react";

import { currentEntry, folderLabel, lastSettledEntry, outcomeLabel, useActivityStore } from "../activity";
import { createSession } from "../api";
import { openPanel } from "../dock";
import {
  buildCards,
  formatSpend,
  pendingCount,
  refusalHint,
  refusalRatio,
  useMissionStore,
  waitingFor,
  type MissionCard,
} from "../mission";
import type { WorkbenchTool } from "../registry";
import { relativeTime } from "../relativeTime";
import { useStore } from "../store";
import type { OrchestratorSnapshot, SessionKind, SpawnRefusal, UsageSessionEntry } from "../types";
import { useUsageStore } from "../usage";
import { statusVisual } from "./Chat";
import { revealPane } from "./Panes";

import "../styles/mission.css";

const TOOL_ID = "mission";

/** The agent tool's id, so a card can reveal the pane that holds its chat.
 * A literal rather than an import: `agent#<session_id>` is the pane-id contract
 * (`ui/src/panes.ts`), and importing `AgentPanel` here would pull the whole chat
 * into this module's chunk to learn one string. */
const AGENT_TOOL_ID = "agent";

/** How often relative stamps are recomputed. `relativeTime` is coarse, but the
 * *waiting* readout on a permission chip is not — a prompt is denied by the
 * server at ten minutes, so the seconds matter here in a way they do not on the
 * activity panel. Five seconds is the compromise: live enough to watch a clock
 * run down, slow enough not to be motion for its own sake (DESIGN.md §5). */
const TICK_MS = 5_000;

function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** DESIGN.md §6.10, as numbers this module can be checked against. */
export const EMPTY_STATE_ICON = { size: 32, strokeWidth: 1.5 } as const;

/** The glyph: a small constellation — a fleet, seen from above. Sized by prop
 * for the same reason the activity pulse is (one shape, three surfaces, three
 * sizes, and a 32px mark drawn at 1.2 reads as a hairline). */
function FleetIcon({ size = 14, strokeWidth = 1.2 }: { size?: number; strokeWidth?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r="2.1" />
      <circle cx="2.6" cy="3.4" r="1.3" />
      <circle cx="13.4" cy="3.4" r="1.3" />
      <circle cx="2.6" cy="12.6" r="1.3" />
      <circle cx="13.4" cy="12.6" r="1.3" />
      <path d="M6.3 6.5 3.7 4.5M9.7 6.5l2.6-2M6.3 9.5l-2.6 2M9.7 9.5l2.6 2" />
    </svg>
  );
}

// ---- the composed view -------------------------------------------------------

/** Everything the board renders, joined from the services that own each half.
 * Exported so the tests can stage fleets a browser journey cannot reach in time
 * (four workers mid-call, one of them blocked). */
export interface MissionView {
  cards: MissionCard[];
  orchestrators: OrchestratorSnapshot | null;
  now: number;
}

function useMission(): MissionView {
  const activity = useActivityStore((s) => s.snapshot);
  const usage = useUsageStore((s) => s.snapshot);
  const orchestrators = useMissionStore((s) => s.orchestrators);
  const permissions = useMissionStore((s) => s.permissions);
  const states = useStore((s) => s.sessionStates);
  const slots = useMissionStore((s) => s.slots);

  useEffect(() => {
    // Each capability starts its own store. The board is a *reader* of three
    // services, so opening it is what makes the two it does not otherwise share
    // a window with start reading too.
    useActivityStore.getState().init();
    useUsageStore.getState().init();
    useMissionStore.getState().init();
  }, []);

  return {
    cards: buildCards({
      sessions: activity?.sessions ?? [],
      orchestrators,
      costs: usage?.sessions ?? EMPTY_COSTS,
      slots,
      permissions,
      states,
    }),
    orchestrators,
    now: useNow(),
  };
}

/** A stable empty array, so a store that has not answered yet does not hand
 * `buildCards` a fresh reference on every render and defeat the memo below it. */
const EMPTY_COSTS: readonly UsageSessionEntry[] = [];

// ---- one card ----------------------------------------------------------------

/** A prompt, answerable here.
 *
 * Two real buttons, because this is the one destructive-adjacent decision the
 * board offers and it has to be reachable from the keyboard (DESIGN.md §7).
 * Deny is not styled as the danger: *allowing* a shell command an agent asked
 * for unattended is the consequential half, so Allow carries the weight and the
 * command it would run is on screen next to it.
 */
function PermissionChips({ card, now }: { card: MissionCard; now: number }) {
  const answer = useMissionStore((s) => s.answer);
  if (card.pending.length === 0) return null;
  return (
    <ul className="wb-mission-prompts">
      {card.pending.map((prompt) => (
        <li key={prompt.request_id} className="wb-mission-prompt">
          <div className="wb-mission-prompt-head">
            <span className="wb-mission-prompt-tool">{prompt.tool}</span>
            <span className="wb-mission-prompt-wait u-tabular" title="Waiting for you">
              {waitingFor(prompt.requested_at, now / 1000)}
            </span>
          </div>
          <p className="wb-mission-prompt-detail">{prompt.description}</p>
          <div className="wb-mission-prompt-actions">
            <button
              type="button"
              className="wb-mission-allow"
              onClick={() => void answer(card.sessionId, prompt.request_id, true)}
            >
              Allow
            </button>
            <button
              type="button"
              className="wb-mission-deny"
              onClick={() => void answer(card.sessionId, prompt.request_id, false)}
            >
              Deny
            </button>
          </div>
        </li>
      ))}
    </ul>
  );
}

// One entry per `SessionKind`, so a new kind is a compile error here rather than
// a card silently mislabelled: `reviewer` used to fall through the old binary
// ternary and render as "Worker" with an unstyled `is-reviewer` class. `chat`
// wears no badge — it is every session's default and would be noise on all of them.
const KIND_LABELS: Record<SessionKind, string | null> = {
  chat: null,
  orchestrator: "Orchestrator",
  worker: "Worker",
  reviewer: "Reviewer",
};

function KindBadge({ card }: { card: MissionCard }) {
  const label = KIND_LABELS[card.kind];
  if (label === null) return null;
  return (
    <span className={`wb-mission-kind is-${card.kind}`} title={`This session is a ${label}`}>
      {label}
    </span>
  );
}

function CardBody({ card, now }: { card: MissionCard; now: number }) {
  const flags = useStore((s) => s.sessionFlags[card.sessionId]);
  const visual = statusVisual(card.state, flags);
  const current = card.activity === null ? null : currentEntry(card.activity);
  const last = card.activity === null ? null : lastSettledEntry(card.activity);
  const blocked = card.pending.length > 0;
  const finished = card.worker?.outcome ?? null;
  return (
    <li
      className={"wb-mission-card" + (blocked ? " is-blocked" : "")}
      data-session={card.sessionId}
      data-kind={card.kind}
    >
      <header className="wb-mission-card-head">
        <span
          className={"wb-dot" + (visual.pulse ? " u-agent-pulse" : "")}
          style={{ background: visual.color }}
          role="img"
          aria-label={visual.label}
        />
        <button
          type="button"
          className="wb-mission-title u-truncate"
          title={`${card.title} — open this conversation`}
          // A card *is* its pane: the session id is the pane key, so this
          // focuses the chat if it is already open and opens it beside the
          // focused pane if it is not (`revealPane`, docs/tools.md).
          onClick={() => revealPane(AGENT_TOOL_ID, card.sessionId)}
        >
          {card.title}
        </button>
        <KindBadge card={card} />
      </header>

      <p className="wb-mission-where u-truncate">
        {card.slot === null ? folderLabel(card.folder) : `${card.slot.slot} · detached`}
        {card.activity !== null && (
          <span className="wb-mission-age u-tabular"> · {relativeTime(card.activity.active_at, now / 1000)}</span>
        )}
      </p>

      <div className="wb-mission-now">
        <span className="u-label wb-mission-phase">Now</span>
        {finished !== null ? (
          <span className={"wb-mission-quiet" + (finished === "failed" ? " is-failed" : "")}>
            {finished === "failed" ? "Failed" : "Stopped"}
            {card.worker?.detail === null || card.worker === null ? "" : ` — ${card.worker.detail}`}
          </span>
        ) : current === null ? (
          <span className="wb-mission-quiet">Nothing running</span>
        ) : (
          <span className="wb-mission-line u-truncate">{current.summary}</span>
        )}
      </div>

      {finished === null && last !== null && (
        <div className="wb-mission-then">
          <span className="u-label wb-mission-phase">Just did</span>
          <span className="wb-mission-line u-truncate">{last.summary}</span>
          <span
            className={"wb-mission-outcome" + (last.ok === false ? " is-failed" : "")}
            style={last.ok === false ? { color: "var(--agent-error)" } : undefined}
          >
            {outcomeLabel(last)}
          </span>
        </div>
      )}

      <dl className="wb-mission-figures">
        <div>
          <dt className="u-label">Turns</dt>
          {/* Absent, not zero: a session that has not finished a turn has spent
              nothing *we know of*, and an em dash says exactly that (§7). */}
          <dd className="u-tabular">{card.cost === null ? "—" : card.cost.turns}</dd>
        </div>
        <div>
          <dt className="u-label">Cost</dt>
          <dd className="u-tabular">{card.cost === null ? "—" : formatSpend(card.cost.cost_usd)}</dd>
        </div>
        <div>
          <dt className="u-label">Worktree</dt>
          <dd className="u-truncate">{card.slot?.slot ?? "—"}</dd>
        </div>
      </dl>

      <PermissionChips card={card} now={now} />
    </li>
  );
}

/**
 * The card is O(1) in renders, not only in what it holds.
 *
 * Four sources feed this board and any of them can replace its snapshot object,
 * so without this boundary one worker's tool call re-renders every other card —
 * several times a second, for rows whose data did not move. That is the
 * re-render storm the server's 250 ms activity coalescing exists to prevent,
 * arriving one layer later, and it gets worse with exactly the thing this
 * feature is for: four agents working at once.
 */
export const MissionCardView = memo(CardBody);

// ---- the panel ---------------------------------------------------------------

export function MissionPanel(_props: IDockviewPanelProps) {
  return <MissionBody {...useMission()} />;
}

/** A ceiling that bound, rendered where the user is looking. */
function RefusalNote({ refusal }: { refusal: SpawnRefusal }) {
  const ratio = refusalRatio(refusal);
  const hint = refusalHint(refusal);
  return (
    <p className="wb-mission-refusal" data-testid="mission-refusal">
      <span className="wb-mission-refusal-label">At the ceiling</span>
      {ratio !== null && <span className="u-tabular wb-mission-refusal-ratio">{ratio}</span>}
      <span className="wb-mission-refusal-detail">{refusal.detail}</span>
      {hint !== null && <span className="wb-mission-refusal-hint">{hint}</span>}
    </p>
  );
}

/** The board's whole rendering, as a function of the composed view. Exported
 * for the tests that stage fleets no browser journey can reach in time. */
export function MissionBody({ cards, orchestrators, now }: MissionView) {
  const stop = useMissionStore((s) => s.stop);
  const blocked = pendingCount(cards);
  const working = cards.filter((card) => card.state === "working").length;
  const crews = orchestrators?.orchestrators ?? [];

  return (
    <div className="wb-mission">
      <header className="wb-mission-head">
        <span className="u-label">Mission Control</span>
        <span className="wb-mission-summary u-tabular">
          {cards.length === 0
            ? "No sessions"
            : `${String(working)} of ${String(cards.length)} working` +
              (blocked === 0 ? "" : ` · ${String(blocked)} waiting on you`)}
        </span>
      </header>

      {cards.length === 0 ? (
        <div className="wb-mission-empty" data-testid="mission-empty">
          <span className="wb-mission-empty-icon">
            <FleetIcon {...EMPTY_STATE_ICON} />
          </span>
          <p className="wb-mission-empty-title">No agents running</p>
          <p className="wb-mission-note">
            Every session appears here the moment it starts — what it is doing, what it has cost,
            and which worktree it holds. A permission an agent is blocked on is answerable from its
            card, including for conversations you have never opened.
          </p>
          <button
            type="button"
            className="wb-mission-start"
            onClick={() => void startOrchestrator()}
            data-testid="mission-start-orchestrator"
          >
            New orchestrator session
          </button>
          <p className="wb-mission-note">
            An orchestrator can spawn workers, each in its own git worktree, under a budget it
            cannot exceed — and it can never approve their shell commands for you.
          </p>
        </div>
      ) : (
        <ul className="wb-mission-fleet">
          {cards.map((card) => (
            <MissionCardView key={card.sessionId} card={card} now={now} />
          ))}
        </ul>
      )}

      {crews.length > 0 && orchestrators !== null && (
        <section className="wb-mission-crews">
          {crews.map((crew) => {
            const running = crew.workers.filter((worker) => worker.outcome === null).length;
            return (
              <div key={crew.orchestrator_id} className="wb-mission-crew">
                <header className="wb-mission-crew-head">
                  <span className="u-label">Crew</span>
                  <span className="wb-mission-crew-count u-tabular">
                    {running} of {orchestrators.budget.max_workers} workers
                  </span>
                  <button
                    type="button"
                    className="wb-mission-stop"
                    title="Stop every worker this orchestrator started, return their worktrees, and interrupt its current turn"
                    onClick={() => void stop(crew.orchestrator_id)}
                  >
                    Stop crew
                  </button>
                </header>
                {crew.last_refusal !== null && <RefusalNote refusal={crew.last_refusal} />}
              </div>
            );
          })}
          <p className="wb-mission-note wb-mission-source">
            Ceilings: {orchestrators.budget.max_workers} workers,{" "}
            {orchestrators.budget.max_fleet_turns} turns and{" "}
            {formatSpend(orchestrators.budget.max_fleet_cost_usd)} per crew;{" "}
            {orchestrators.max_concurrent_sessions} sessions working at once
            {orchestrators.worktree_capacity === null
              ? " (no worktree pool — this workspace is not a git repository, so a crew has nowhere safe to work)"
              : `, ${String(orchestrators.worktree_capacity)} worktree slots`}
            . Activity and cost come from the live feed and the usage service — this board renders
            them, it does not keep its own copy.
          </p>
        </section>
      )}
    </div>
  );
}

// ---- status bar ---------------------------------------------------------------

/**
 * The glance: how many agents are waiting on *you*.
 *
 * Renders nothing when nothing is blocked (§6.7, counts hide at zero), which is
 * the common case — and is deliberately not another "sessions working" count,
 * because the Agent tool and the activity reading already carry two of those.
 * This is the one number nothing else in the window can tell you.
 */
function MissionStatus() {
  return <MissionReading {...useMission()} />;
}

export function MissionReading({ cards }: MissionView) {
  const blocked = pendingCount(cards);
  if (blocked === 0) return null;
  const waiting = cards.filter((card) => card.pending.length > 0);
  const title = [
    `${String(blocked)} ${blocked === 1 ? "agent is" : "agents are"} waiting for a decision`,
    ...waiting.map((card) => `${card.title}: ${card.pending[0]?.tool ?? ""}`),
    "Open Mission Control",
  ].join(" — ");
  return (
    <button
      type="button"
      className="wb-mission-status"
      title={title}
      onClick={() => openPanel(TOOL_ID)}
      data-testid="mission-status"
    >
      <span className="wb-mission-status-icon">
        <FleetIcon />
      </span>
      <span className="u-tabular">{blocked}</span>
    </button>
  );
}

// ---- starting one ------------------------------------------------------------

/**
 * Start an orchestrator, open its chat, and put the board beside it.
 *
 * The one thing on this board that *creates* rather than renders, and it is
 * here rather than on the Agent tool because the session kind is Mission
 * Control's. Everything after the POST is the shipped machinery: the session
 * becomes an ordinary live session, `revealPane` binds a pane to it by id, and
 * the board picks it up from the activity feed like any other.
 */
export async function startOrchestrator(): Promise<string | null> {
  try {
    const info = await createSession({ folder: "", kind: "orchestrator" });
    useStore.getState().openLiveSession(info);
    void useStore.getState().refreshSessions();
    revealPane(AGENT_TOOL_ID, info.session_id);
    openPanel(TOOL_ID);
    return info.session_id;
  } catch (err) {
    // The concurrency cap is the likely refusal, and it is the server's
    // sentence — a client-side guess would grey at the wrong moment.
    useStore.getState().pushToast("error", `Could not start an orchestrator: ${String(err)}`);
    return null;
  }
}

// ---- registration -------------------------------------------------------------

export const missionTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Mission Control",
  icon: FleetIcon,
  panel: {
    component: MissionPanel,
    defaultLocation: { area: "right", size: 400 },
    // Not in the startup layout: this is what you open when you are running a
    // crew, and the status reading is the always-on half. Singleton — every
    // pane of it would render the same fleet, and a pane needs something to be
    // *pointed at* to be worth having twice (the same call the Activity panel
    // made, and the reason neither declares `instances`).
    openByDefault: false,
  },
  commands: [
    {
      id: "mission.open",
      title: "Show Mission Control",
      detail: () => "every agent, what it is doing, what it cost, and what it is waiting for",
      run: () => openPanel(TOOL_ID),
    },
    {
      id: "mission.newOrchestrator",
      title: "New orchestrator session",
      detail: () => "an agent that can spawn workers, each in its own worktree",
      // Mission Control's command, not the Agent's: an orchestrator is *this*
      // capability's session kind, and putting it on the Agent tool would mean
      // editing another capability's module to add one of ours.
      run: () => void startOrchestrator(),
    },
  ],
  // No chord: a registered chord beats a user's `shortcuts.md` one and `Alt` is
  // all that file may bind. This is a board you open, not a hot path.
  statusContributions: [{ region: "right", component: MissionStatus }],
};
