/**
 * The rules that make the usage surface honest, held one at a time.
 *
 * Every case here is one a lazier meter would get wrong: treating "not
 * reported" as zero, ageing a figure with the browser's clock instead of the
 * server's, letting a colour carry a warning on its own, or showing the calmest
 * window in the status bar while another one is at its cap.
 */

import { describe, expect, it } from "vitest";

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
} from "./usage";
import type { UsageBucket, UsageBucketKind, UsageSnapshot } from "./types";

const OBSERVED = 1_700_000_000;
const RECEIVED_MS = 5_000_000;

function bucket(patch: Partial<UsageBucket> = {}): UsageBucket {
  return {
    kind: "five_hour",
    status: "allowed",
    utilization: 0.4,
    resets_at: null,
    observed_at: OBSERVED,
    ...patch,
  };
}

function snapshot(patch: Partial<UsageSnapshot> = {}): UsageSnapshot {
  const buckets = patch.buckets ?? [bucket()];
  return {
    buckets,
    overage: null,
    observed_at: OBSERVED,
    age_s: 0,
    session_cost: {
      turns: 0,
      total_cost_usd: 0,
      last_turn_cost_usd: null,
      models: [],
      observed_at: null,
    },
    ...patch,
  };
}

describe("how loud a bucket is", () => {
  it("takes the SDK's own status over the number", () => {
    // The CLI said "approaching" at 12%; that is its call to make, not ours.
    expect(usageLevel(bucket({ status: "allowed_warning", utilization: 0.12 }))).toBe("warning");
    expect(usageLevel(bucket({ status: "rejected", utilization: 0.5 }))).toBe("critical");
  });

  it("falls back to display thresholds when the status has not spoken", () => {
    expect(usageLevel(bucket({ utilization: 0.74 }))).toBe("normal");
    expect(usageLevel(bucket({ utilization: 0.75 }))).toBe("warning");
    expect(usageLevel(bucket({ utilization: 0.9 }))).toBe("critical");
  });

  it("stays quiet when there is no figure at all", () => {
    // "Not reported" is not "0% used" and it is not "nearly full" either.
    expect(usageLevel(bucket({ utilization: null }))).toBe("normal");
    expect(levelNote(bucket({ utilization: null }))).toBeNull();
  });

  it("gives every colour a word to go with it (DESIGN.md §7)", () => {
    expect(levelNote(bucket({ utilization: 0.4 }))).toBeNull();
    expect(levelNote(bucket({ utilization: 0.8 }))).toBe("Approaching limit");
    expect(levelNote(bucket({ utilization: 0.95 }))).toBe("At the limit");
    // A refused request says so outright rather than being read off a bar.
    expect(levelNote(bucket({ status: "rejected", utilization: 0.95 }))).toBe("Limit reached");
  });
});

describe("the bucket the status bar shows", () => {
  it("is nothing when nothing has been reported", () => {
    expect(peakBucket([])).toBeNull();
  });

  it("is the loudest, then the fullest", () => {
    const calm = bucket({ kind: "five_hour", utilization: 0.2 });
    const busy = bucket({ kind: "seven_day", utilization: 0.6 });
    const warned = bucket({ kind: "seven_day_opus", status: "allowed_warning", utilization: 0.1 });
    expect(peakBucket([calm, busy])?.kind).toBe("seven_day");
    // A warned window beats a fuller one that the CLI is still happy with.
    expect(peakBucket([calm, busy, warned])?.kind).toBe("seven_day_opus");
  });

  it("breaks a tie towards the window that bites first", () => {
    // Buckets arrive in presentation order, so "first wins" is "shortest wins".
    const five = bucket({ kind: "five_hour", utilization: 0.5 });
    const week = bucket({ kind: "seven_day", utilization: 0.5 });
    expect(peakBucket([five, week])?.kind).toBe("five_hour");
  });
});

describe("age, on the server's clock", () => {
  it("adds local elapsed time to the age the server measured", () => {
    const snap = snapshot({ age_s: 120 });
    expect(ageSeconds(snap, RECEIVED_MS, RECEIVED_MS)).toBe(120);
    expect(ageSeconds(snap, RECEIVED_MS, RECEIVED_MS + 60_000)).toBe(180);
  });

  it("never subtracts one clock from another", () => {
    // A browser an hour behind the server must not report the figure as an
    // hour *newer* than it is — the age comes from the server plus elapsed.
    const snap = snapshot({ age_s: 30, observed_at: OBSERVED });
    expect(ageSeconds(snap, RECEIVED_MS, RECEIVED_MS)).toBe(30);
    expect(serverNow(snap, RECEIVED_MS, RECEIVED_MS)).toBe(OBSERVED + 30);
  });

  it("is null when nothing has ever been observed", () => {
    const empty = snapshot({ buckets: [], observed_at: null, age_s: null });
    expect(ageSeconds(empty, RECEIVED_MS, RECEIVED_MS)).toBeNull();
    expect(serverNow(empty, RECEIVED_MS, RECEIVED_MS)).toBeNull();
    expect(formatAge(null)).toBe("Never updated");
  });

  it("ages each window separately", () => {
    // The weekly figure is routinely hours older than the five-hour one; a
    // single snapshot stamp would hide that.
    const stale = bucket({ kind: "seven_day", observed_at: OBSERVED - 7200 });
    const fresh = bucket({ kind: "five_hour", observed_at: OBSERVED });
    const snap = snapshot({ buckets: [fresh, stale], age_s: 60 });
    expect(bucketAgeSeconds(snap, fresh, RECEIVED_MS, RECEIVED_MS)).toBe(60);
    expect(bucketAgeSeconds(snap, stale, RECEIVED_MS, RECEIVED_MS)).toBe(7260);
  });

  it("clamps a clock that jumped backwards", () => {
    const snap = snapshot({ age_s: 10 });
    expect(ageSeconds(snap, RECEIVED_MS, RECEIVED_MS - 60_000)).toBe(10);
  });
});

describe("formatting", () => {
  it("shows a missing figure as a dash, never as zero", () => {
    expect(formatUtilization(null)).toBe("—");
    expect(formatUtilization(0)).toBe("0%");
    expect(formatUtilization(0.426)).toBe("43%");
  });

  it("clamps the bar without clamping the number", () => {
    // If the CLI ever reported more than the window, the text says so and the
    // bar simply fills — a 140% bar would draw outside its track.
    expect(barFraction(1.4)).toBe(1);
    expect(formatUtilization(1.4)).toBe("140%");
    expect(barFraction(null)).toBe(0);
  });

  it("reads durations coarsely", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(59)).toBe("59s");
    expect(formatDuration(600)).toBe("10m");
    expect(formatDuration(3600)).toBe("1h");
    expect(formatDuration(7500)).toBe("2h 5m");
    expect(formatDuration(86_400)).toBe("1d");
    expect(formatDuration(280_000)).toBe("3d 5h");
  });

  it("stamps the panel header in words", () => {
    expect(formatAge(0)).toBe("Updated just now");
    expect(formatAge(300)).toBe("Updated 5m ago");
  });

  it("puts reset times on the server's clock", () => {
    expect(formatReset(null, OBSERVED)).toBeNull();
    expect(formatReset(OBSERVED + 7500, OBSERVED)).toBe("Resets in 2h 5m");
    expect(formatReset(OBSERVED - 5, OBSERVED)).toBe("Reset due");
    // Nothing observed means no server clock to measure against, and guessing
    // with the browser's would be the bug this whole module avoids.
    expect(formatReset(OBSERVED + 100, null)).toBe("Reset time unknown");
  });

  it("formats the degraded view's own numbers", () => {
    expect(formatCost(0.0125)).toBe("$0.0125");
    expect(formatCost(12.5)).toBe("$12.50");
    expect(formatTokens(999)).toBe("999");
    expect(formatTokens(12_400)).toBe("12.4k");
    expect(formatTokens(2_500_000)).toBe("2.5M");
  });
});

describe("naming", () => {
  it("names every window the schema allows, long and short", () => {
    const kinds: UsageBucketKind[] = [
      "five_hour",
      "seven_day",
      "seven_day_opus",
      "seven_day_sonnet",
      "overage",
      "unspecified",
    ];
    for (const kind of kinds) {
      expect(bucketLabel(kind).length).toBeGreaterThan(0);
      // The status bar is 24px and shares it with everything else.
      expect(bucketShortLabel(kind).length).toBeLessThanOrEqual(9);
    }
    expect(bucketLabel("seven_day")).toBe("Weekly, all models");
    // An event that named no window says so rather than borrowing a name.
    expect(bucketLabel("unspecified")).toBe("Unnamed limit");
  });
});
