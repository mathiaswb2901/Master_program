/**
 * Claude plan usage on the UI side: the arithmetic, and a store next to it.
 *
 * Everything a meter needs to say is computed here, as pure functions over the
 * snapshot the server sends — so the rules that make this feature *honest* are
 * unit-tested rather than trusted (`usage.test.ts`):
 *
 * - **A figure is stale until an agent talks.** The snapshot arrives with an
 *   `age_s` the server measured, and ages further while the page sits open. Age
 *   is therefore server age + local *elapsed* time, never a subtraction between
 *   two clocks (`ageSeconds`). Same for reset times: `serverNow` puts "resets
 *   in 2 h" on the server's clock, so a browser five minutes fast does not
 *   invent five minutes of headroom.
 * - **No data is not zero.** `utilization: null` and `buckets: []` are distinct
 *   states with their own renderings; neither may draw an empty bar.
 * - **Nothing is synthesized.** The only judgement here is where a bar starts
 *   *looking* alarming (`usageLevel`), and even that defers to the status the
 *   SDK itself reported.
 *
 * The store is this tool's own zustand instance rather than a slice of
 * `store.ts`: nothing outside this capability reads it, which is exactly the
 * condition CLAUDE.md puts on a second store. It subscribes to the shared
 * `/ws/events` socket for its own frames — the same thing `officeHost.ts` does.
 */

import { create } from "zustand";

import * as api from "./api";
import type { UsageBucket, UsageBucketKind, UsageSnapshot, WorkspaceEvent } from "./types";
import { ReconnectingSocket } from "./ws";

// ---- naming ------------------------------------------------------------------

/** Panel label per window. Long form: the meter has room for a sentence. */
const BUCKET_LABELS: Record<UsageBucketKind, string> = {
  five_hour: "5-hour limit",
  seven_day: "Weekly, all models",
  seven_day_opus: "Weekly, Opus",
  seven_day_sonnet: "Weekly, Sonnet",
  overage: "Overage",
  unspecified: "Unnamed limit",
};

/** Status-bar label per window: the bar is 24px and shares it with everything. */
const BUCKET_SHORT: Record<UsageBucketKind, string> = {
  five_hour: "5h",
  seven_day: "7d",
  seven_day_opus: "7d Opus",
  seven_day_sonnet: "7d Sonnet",
  overage: "Overage",
  unspecified: "Limit",
};

export const bucketLabel = (kind: UsageBucketKind): string => BUCKET_LABELS[kind];
export const bucketShortLabel = (kind: UsageBucketKind): string => BUCKET_SHORT[kind];

// ---- how loud a bucket is ----------------------------------------------------

export type UsageLevel = "normal" | "warning" | "critical";

/**
 * Where a bar starts earning attention. These are *display* thresholds, not
 * claims about the plan: the SDK never says "you are at 75%, be careful", it
 * says `allowed`/`allowed_warning`/`rejected`. We honour that status first and
 * only use the number when the status has not spoken.
 */
export const WARN_AT = 0.75;
export const CRITICAL_AT = 0.9;

export function usageLevel(bucket: UsageBucket): UsageLevel {
  if (bucket.status === "rejected") return "critical";
  if (bucket.status === "allowed_warning") return "warning";
  const used = bucket.utilization;
  if (used === null) return "normal";
  if (used >= CRITICAL_AT) return "critical";
  if (used >= WARN_AT) return "warning";
  return "normal";
}

/**
 * The words that go with the colour. DESIGN.md §7 forbids colour as the only
 * signal, so every bar that is not `normal` carries this next to it — and a
 * `rejected` status says so outright rather than being read off a percentage.
 */
export function levelNote(bucket: UsageBucket): string | null {
  if (bucket.status === "rejected") return "Limit reached";
  const level = usageLevel(bucket);
  if (level === "critical") return "At the limit";
  if (level === "warning") return "Approaching limit";
  return null;
}

/**
 * The bucket the status bar shows: loudest first, then fullest. Buckets arrive
 * in a fixed order, so equal candidates resolve to the shortest window — the
 * one that bites first.
 */
export function peakBucket(buckets: readonly UsageBucket[]): UsageBucket | null {
  const rank: Record<UsageLevel, number> = { normal: 0, warning: 1, critical: 2 };
  let best: UsageBucket | null = null;
  for (const bucket of buckets) {
    if (best === null) {
      best = bucket;
      continue;
    }
    const delta = rank[usageLevel(bucket)] - rank[usageLevel(best)];
    if (delta > 0 || (delta === 0 && (bucket.utilization ?? -1) > (best.utilization ?? -1))) {
      best = bucket;
    }
  }
  return best;
}

// ---- time, on the server's clock --------------------------------------------

/**
 * How old the snapshot is now: the age the server measured when it built it,
 * plus the time this page has held it. Never `Date.now() - observed_at` — that
 * subtracts two clocks and reports the difference between them as staleness.
 */
export function ageSeconds(
  snapshot: UsageSnapshot,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  if (snapshot.age_s === null) return null;
  return snapshot.age_s + Math.max(0, (nowMs - receivedAtMs) / 1000);
}

/** The same, per bucket: windows transition independently, so the weekly figure
 * is routinely hours older than the five-hour one and the panel says so. */
export function bucketAgeSeconds(
  snapshot: UsageSnapshot,
  bucket: UsageBucket,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  const snapshotAge = ageSeconds(snapshot, receivedAtMs, nowMs);
  if (snapshotAge === null || snapshot.observed_at === null) return null;
  // Both stamps come from the server clock, so their difference is exact.
  return snapshotAge + Math.max(0, snapshot.observed_at - bucket.observed_at);
}

/** Unix seconds *as the server would read them* right now. */
export function serverNow(
  snapshot: UsageSnapshot,
  receivedAtMs: number,
  nowMs: number,
): number | null {
  const age = ageSeconds(snapshot, receivedAtMs, nowMs);
  if (age === null || snapshot.observed_at === null) return null;
  return snapshot.observed_at + age;
}

// ---- formatting --------------------------------------------------------------

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

/** "42%" — or an em dash when the event carried no figure, which is not zero. */
export function formatUtilization(used: number | null): string {
  return used === null ? "—" : `${Math.round(used * 100).toString()}%`;
}

/** Bar width, clamped to the track. The *number* is never clamped: a CLI that
 * reported 1.4 would still read "140%", because that is what it said. */
export function barFraction(used: number | null): number {
  if (used === null) return 0;
  return Math.min(1, Math.max(0, used));
}

/** "2h 5m", "3d", "45s" — coarse, because these label a meter, not a countdown. */
export function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total < MINUTE) return `${total.toString()}s`;
  if (total < HOUR) return `${Math.floor(total / MINUTE).toString()}m`;
  if (total < DAY) {
    const hours = Math.floor(total / HOUR);
    const minutes = Math.floor((total % HOUR) / MINUTE);
    return minutes === 0 ? `${hours.toString()}h` : `${hours.toString()}h ${minutes.toString()}m`;
  }
  const days = Math.floor(total / DAY);
  const hours = Math.floor((total % DAY) / HOUR);
  return hours === 0 ? `${days.toString()}d` : `${days.toString()}d ${hours.toString()}h`;
}

/** "Updated 4m ago" / "Updated just now" — the stamp caveat 1 demands. */
export function formatAge(seconds: number | null): string {
  if (seconds === null) return "Never updated";
  return seconds < 45 ? "Updated just now" : `Updated ${formatDuration(seconds)} ago`;
}

/** "Resets in 2h 5m", or null when the event carried no reset time. */
export function formatReset(resetsAt: number | null, now: number | null): string | null {
  if (resetsAt === null) return null;
  if (now === null) return "Reset time unknown";
  const remaining = resetsAt - now;
  return remaining <= 0 ? "Reset due" : `Resets in ${formatDuration(remaining)}`;
}

/** "$0.0125" — the degraded view's number, always labelled as session cost. */
export function formatCost(usd: number): string {
  return `$${usd.toFixed(usd < 1 ? 4 : 2)}`;
}

/** "1.2k", "340" — token counts in the degraded view. */
export function formatTokens(count: number): string {
  if (count < 1000) return count.toString();
  if (count < 1_000_000) return `${(count / 1000).toFixed(1)}k`;
  return `${(count / 1_000_000).toFixed(1)}M`;
}

// ---- the store ---------------------------------------------------------------

interface UsageStore {
  /** Null until the first fetch answers — distinct from an empty snapshot,
   * which is a server that answered "nothing has ever arrived". */
  snapshot: UsageSnapshot | null;
  /** `Date.now()` when the current snapshot landed, for the age arithmetic. */
  receivedAt: number;
  init: () => void;
  refresh: () => Promise<void>;
}

let started = false;

export const useUsageStore = create<UsageStore>((set, get) => ({
  snapshot: null,
  receivedAt: 0,

  init: () => {
    if (started) return;
    started = true;
    void get().refresh();
    // Usage rides the shared bus; this socket reads only its own frames. A
    // second subscriber is what the bus is for, and it keeps the app's store
    // free of a capability it does not own.
    new ReconnectingSocket("/ws/events", {
      onMessage: (data) => {
        const event = data as WorkspaceEvent;
        if (event.type !== "usage") return;
        set({ snapshot: event.snapshot, receivedAt: Date.now() });
      },
      // Every reconnect re-reads: frames that arrived while the socket was
      // down are gone, and there is nothing to poll for a fresh figure.
      onOpen: () => {
        void get().refresh();
      },
    });
  },

  refresh: async () => {
    try {
      set({ snapshot: await api.getUsage(), receivedAt: Date.now() });
    } catch {
      // A server that cannot answer leaves the last snapshot standing with its
      // age still ticking up — which is the honest reading of the situation.
    }
  },
}));
