/**
 * Your Claude plan limits, inside Workbench.
 *
 * A whole capability in one module (plus its stylesheet and `usage.ts`): the
 * panel, the status-bar reading, the command that opens it, and the descriptor
 * that registers all three. It edits no shared file except the one line in
 * `tools.ts` that registers it.
 *
 * **The honest part of this feature is on screen, not in a comment.** The
 * figures come from one place — a rate-limit transition on a live session's
 * SDK stream — and that source has four properties a user has to be able to
 * see, so each one has a rendering rather than a caveat in the docs:
 *
 * 1. *Stale until you talk to an agent.* Every meter carries when its figure
 *    arrived, the header carries the newest stamp, and a snapshot older than
 *    `STALE_AFTER_S` says so in words next to the numbers. A figure is never
 *    dressed up as current.
 * 2. *No polling.* There is no refresh button, because there is nothing to
 *    ask; the panel says where the numbers come from instead of implying a
 *    button would fetch new ones.
 * 3. *An account may never emit one.* The empty state is a designed surface,
 *    not an error — it explains why it is empty and falls back to what we do
 *    know: what this server has spent, labelled session cost.
 * 4. *No synthesis.* A window the SDK never mentioned is absent; a bucket with
 *    no `utilization` shows its status and a dash, never a zeroed bar.
 *
 * DESIGN.md §7: colour is never the only signal, so a bar that has earned a
 * colour also carries the words ("Approaching limit", "Limit reached").
 */

import type { IDockviewPanelProps } from "dockview";
import { useEffect, useState } from "react";

import { openPanel } from "../dock";
import type { WorkbenchTool } from "../registry";
import type { UsageBucket, UsageSnapshot } from "../types";
import {
  ageSeconds,
  barFraction,
  bucketAgeSeconds,
  bucketLabel,
  bucketShortLabel,
  formatAge,
  formatCost,
  formatDuration,
  formatReset,
  formatTokens,
  formatUtilization,
  levelNote,
  peakBucket,
  serverNow,
  usageLevel,
  useUsageStore,
} from "../usage";

import "../styles/usage.css";

const TOOL_ID = "usage";

/** How often the relative stamps are recomputed. A minute: these are coarse
 * ("4m ago"), and a ticking clock in a status bar is exactly the kind of motion
 * DESIGN.md §5 rules out. */
const TICK_MS = 60_000;

/** Past this, the panel stops presenting the figure as a reading and starts
 * saying it is old. Fifteen minutes because the five-hour window is the one
 * that moves fastest, and a quarter of an hour of agent work can move it a lot. */
export const STALE_AFTER_S = 15 * 60;

/** Re-render on a slow tick so "4m ago" becomes "5m ago" without an event. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/**
 * What both surfaces render from. A plain object rather than each component
 * reaching into the store, so the *views* below are pure functions of a
 * snapshot and a pair of clock readings — which is what makes a fifteen-minute
 * stale snapshot something a test can render in a millisecond instead of a
 * journey that has to wait for it.
 */
export interface UsageView {
  snapshot: UsageSnapshot | null;
  /** `Date.now()` when the snapshot arrived. */
  receivedAt: number;
  /** `Date.now()` now. Held in state and ticked, never read mid-render. */
  now: number;
}

/** The store, started by whichever surface mounts first (the status item is
 * always mounted, so in practice that is boot). */
function useUsage(): UsageView {
  const snapshot = useUsageStore((s) => s.snapshot);
  const receivedAt = useUsageStore((s) => s.receivedAt);
  useEffect(() => {
    useUsageStore.getState().init();
  }, []);
  return { snapshot, receivedAt, now: useNow() };
}

function GaugeIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2 11.5a6 6 0 1 1 12 0" />
      <path d="M8 11.5 11 6.5" />
    </svg>
  );
}

// ---- one meter ---------------------------------------------------------------

function Meter({
  bucket,
  snapshot,
  receivedAt,
  now,
}: {
  bucket: UsageBucket;
  snapshot: UsageSnapshot;
  receivedAt: number;
  now: number;
}) {
  const level = usageLevel(bucket);
  const note = levelNote(bucket);
  const label = bucketLabel(bucket.kind);
  const value = formatUtilization(bucket.utilization);
  const reset = formatReset(bucket.resets_at, serverNow(snapshot, receivedAt, now));
  const age = bucketAgeSeconds(snapshot, bucket, receivedAt, now);
  return (
    <section className={`wb-usage-meter is-${level}`}>
      <div className="wb-usage-meter-head">
        <span className="wb-usage-meter-label u-truncate">{label}</span>
        <span className="wb-usage-meter-value u-tabular">{value}</span>
      </div>
      <div
        className="wb-usage-track"
        role="meter"
        aria-label={`${label}: ${value} used`}
        aria-valuenow={bucket.utilization === null ? undefined : Math.round(bucket.utilization * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuetext={bucket.utilization === null ? "not reported" : value}
      >
        <div
          className="wb-usage-fill"
          style={{ width: `${(barFraction(bucket.utilization) * 100).toString()}%` }}
        />
      </div>
      <div className="wb-usage-meter-foot">
        {/* The label that keeps colour from being the only signal (§7). */}
        {note !== null && <span className="wb-usage-meter-note">{note}</span>}
        {reset !== null && <span className="wb-usage-meter-reset">{reset}</span>}
        <span className="wb-usage-meter-age" title="When this figure arrived">
          {age === null ? "—" : `${formatDuration(age)} old`}
        </span>
      </div>
    </section>
  );
}

/**
 * Pay-as-you-go, when an event carried any. Its own line rather than a sixth
 * meter: the SDK reports it as a status (and sometimes a reason in prose), not
 * as a fraction of anything, so there is no bar to draw honestly.
 */
function Overage({
  snapshot,
  receivedAt,
  now,
}: {
  snapshot: UsageSnapshot;
  receivedAt: number;
  now: number;
}) {
  const overage = snapshot.overage;
  if (overage === null) return null;
  const reset = formatReset(overage.resets_at, serverNow(snapshot, receivedAt, now));
  return (
    <p className="wb-usage-note wb-usage-overage">
      Overage: {overage.status === "rejected" ? "unavailable" : "available"}
      {overage.disabled_reason === null ? "" : ` — ${overage.disabled_reason}`}
      {reset === null ? "" : `. ${reset}`}
    </p>
  );
}

// ---- the degraded view -------------------------------------------------------

/**
 * What we know when the SDK has told us nothing: money this server watched go
 * by. Rendered under its own heading in both states — it is never the answer to
 * "how much of my plan is left", and it says so.
 */
function SessionCost({ snapshot }: { snapshot: UsageSnapshot }) {
  const cost = snapshot.session_cost;
  if (cost.turns === 0) {
    return (
      <p className="wb-usage-note">
        No turns have finished in this workbench yet, so there is not even a session cost to show.
      </p>
    );
  }
  return (
    <>
      <div className="wb-usage-cost-row">
        <span className="wb-usage-cost-value u-tabular">{formatCost(cost.total_cost_usd)}</span>
        <span className="wb-usage-note">
          over {cost.turns} {cost.turns === 1 ? "turn" : "turns"} since this server started
          {cost.last_turn_cost_usd === null
            ? ""
            : ` — last turn ${formatCost(cost.last_turn_cost_usd)}`}
        </span>
      </div>
      {cost.models.length > 0 && (
        <ul className="wb-usage-models">
          {cost.models.map((entry) => (
            <li key={entry.model} className="wb-usage-model">
              <span className="wb-usage-model-name u-truncate">{entry.model}</span>
              <span className="wb-usage-model-figures u-tabular">
                {formatCost(entry.cost_usd)} · {formatTokens(entry.input_tokens)} in ·{" "}
                {formatTokens(entry.output_tokens)} out
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// ---- the panel ---------------------------------------------------------------

export function UsagePanel(_props: IDockviewPanelProps) {
  return <UsageBody {...useUsage()} />;
}

/** The panel's whole rendering, as a function of the view. Exported for the
 * tests that hold the states a browser journey cannot reach in time. */
export function UsageBody({ snapshot, receivedAt, now }: UsageView) {
  if (snapshot === null) {
    return (
      <div className="wb-usage">
        <p className="wb-usage-note">Reading usage…</p>
      </div>
    );
  }
  const age = ageSeconds(snapshot, receivedAt, now);
  const hasPlanUsage = snapshot.buckets.length > 0;
  const stale = age !== null && age > STALE_AFTER_S;
  return (
    <div className="wb-usage">
      <header className="wb-usage-head">
        <span className="u-label">Claude plan usage</span>
        <span className={`wb-usage-stamp u-tabular${stale ? " is-stale" : ""}`}>
          {formatAge(age)}
        </span>
      </header>

      {hasPlanUsage ? (
        <>
          {stale && (
            <p className="wb-usage-warning" role="status">
              These figures are {formatDuration(age ?? 0)} old. Claude reports a limit only when it
              changes, on a live session&apos;s stream — there is nothing to poll, so they stay as
              they are until you talk to an agent again.
            </p>
          )}
          <div className="wb-usage-meters">
            {snapshot.buckets.map((bucket) => (
              <Meter
                key={bucket.kind}
                bucket={bucket}
                snapshot={snapshot}
                receivedAt={receivedAt}
                now={now}
              />
            ))}
          </div>
          <Overage snapshot={snapshot} receivedAt={receivedAt} now={now} />
          <p className="wb-usage-source">
            Only the windows Claude has reported are shown — one arrives per limit, when that limit
            changes. Nothing here is estimated.
          </p>
        </>
      ) : (
        <div className="wb-usage-empty" data-testid="usage-empty">
          <span className="wb-usage-empty-icon">
            <GaugeIcon />
          </span>
          <p className="wb-usage-empty-title">No plan usage reported yet</p>
          <p className="wb-usage-note">
            Claude sends your 5-hour and weekly limits only when one of them changes, on a live
            session&apos;s stream. There is no way to ask for them, and some accounts never send
            them at all. Until one arrives, this is what Workbench actually knows:
          </p>
        </div>
      )}

      <section className="wb-usage-degraded">
        <h2 className="u-label">Session cost — not plan usage</h2>
        <SessionCost snapshot={snapshot} />
      </section>
    </div>
  );
}

// ---- status bar --------------------------------------------------------------

/**
 * The number you glance at. Quiet while there is room: `--text-tertiary`, no
 * fill, the way the rest of the bar reads. It earns a tint at
 * `allowed_warning` (or 75%) and the semantic error colour once a limit is
 * reached — and never colour alone: the label names the window and the tooltip
 * spells out the state and its age.
 *
 * Renders **nothing** when no limit has ever been reported, which is the same
 * rule as the session counts next to it (§6.7, "counts hide at zero"). The
 * capability is still reachable — the QuickBar command opens the panel, where
 * the empty state explains itself.
 */
function UsageStatus() {
  return <UsageReading {...useUsage()} />;
}

export function UsageReading({ snapshot, receivedAt, now }: UsageView) {
  const bucket = snapshot === null ? null : peakBucket(snapshot.buckets);
  if (snapshot === null || bucket === null) return null;
  const level = usageLevel(bucket);
  const note = levelNote(bucket);
  const age = ageSeconds(snapshot, receivedAt, now);
  const stale = age !== null && age > STALE_AFTER_S;
  const title = [
    `${bucketLabel(bucket.kind)}: ${formatUtilization(bucket.utilization)} used`,
    note,
    formatReset(bucket.resets_at, serverNow(snapshot, receivedAt, now)),
    formatAge(age),
    "Open the usage panel",
  ]
    .filter((part): part is string => part !== null)
    .join(" — ");
  return (
    <button
      type="button"
      className={`wb-usage-status is-${level}${stale ? " is-stale" : ""}`}
      title={title}
      onClick={() => openPanel(TOOL_ID)}
    >
      <span className="wb-usage-status-label">{bucketShortLabel(bucket.kind)}</span>
      <span className="wb-usage-status-value u-tabular">
        {formatUtilization(bucket.utilization)}
      </span>
      {note !== null && <span className="wb-usage-status-note">{note}</span>}
    </button>
  );
}

// ---- registration ------------------------------------------------------------

export const usageTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Usage",
  icon: GaugeIcon,
  panel: {
    component: UsagePanel,
    defaultLocation: { area: "right", size: 380 },
    // Not in the startup layout: a meter is something you check, not something
    // that takes a pane from your work. The status reading is always there.
    openByDefault: false,
  },
  commands: [
    {
      id: "usage.open",
      title: "Show Claude plan usage",
      detail: () => "5-hour and weekly limits, as Claude last reported them",
      run: () => openPanel(TOOL_ID),
    },
  ],
  // No chord: a registered chord beats a user's `shortcuts.md` one, and Alt is
  // all that file may bind. This is a glance, not a hot path (see Scratchpad).
  statusContributions: [{ region: "right", component: UsageStatus }],
};
