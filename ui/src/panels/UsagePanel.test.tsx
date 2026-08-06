/**
 * The panel's three states, rendered.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design —
 * `vitest.config.ts`): what is under test is *what the panel says*, and every
 * branch here is a claim about honesty rather than about interaction.
 *
 * The stale case is here and not in the E2E suite for a plain reason: a
 * snapshot has to be a quarter of an hour old to be stale, and no browser
 * journey may sit and wait for that. The E2E suite covers what it can reach —
 * fresh meters, the age stamp, and the empty state.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { UsageSnapshot } from "../types";

import { STALE_AFTER_S, UsageBody, UsageReading } from "./UsagePanel";

// The one import that reaches outside this capability: `openPanel` pulls in the
// whole registry, and with it Monaco's browser bundle. Nothing here clicks it.
vi.mock("../dock", () => ({ openPanel: () => undefined }));

const OBSERVED = 1_700_000_000;

const EMPTY_COST = {
  turns: 0,
  total_cost_usd: 0,
  last_turn_cost_usd: null,
  models: [],
  observed_at: null,
};

const NOW_MS = 5_000_000;

/** The panel's body, as it would render `heldForMs` after the snapshot landed. */
function show(snapshot: UsageSnapshot | null, heldForMs = 0): string {
  return renderToStaticMarkup(
    <UsageBody snapshot={snapshot} receivedAt={NOW_MS - heldForMs} now={NOW_MS} />,
  );
}

/** …and the status bar's reading of the same view. */
function reading(snapshot: UsageSnapshot | null, heldForMs = 0): string {
  return renderToStaticMarkup(
    <UsageReading snapshot={snapshot} receivedAt={NOW_MS - heldForMs} now={NOW_MS} />,
  );
}

const NEVER: UsageSnapshot = {
  buckets: [],
  overage: null,
  observed_at: null,
  age_s: null,
  session_cost: EMPTY_COST,
  sessions: [],
};

const FRESH: UsageSnapshot = {
  buckets: [
    {
      kind: "five_hour",
      status: "allowed",
      utilization: 0.42,
      resets_at: OBSERVED + 7200,
      observed_at: OBSERVED,
    },
    {
      kind: "seven_day",
      status: "allowed_warning",
      utilization: 0.86,
      resets_at: OBSERVED + 200_000,
      observed_at: OBSERVED,
    },
  ],
  overage: null,
  observed_at: OBSERVED,
  age_s: 5,
  session_cost: EMPTY_COST,
  sessions: [],
};

describe("the panel with no plan usage at all", () => {
  it("says why it is empty instead of showing zeroes", () => {
    const html = show(NEVER);
    expect(html).toContain("No plan usage reported yet");
    expect(html).toContain("Never updated");
    // The reason, in the user's words: it is a transition event, unaskable.
    expect(html).toContain("only when one of them changes");
    expect(html).toContain("no way to ask for them");
    // Not a single meter, and above all not a 0% one.
    expect(html).not.toContain("wb-usage-track");
    expect(html).not.toContain("0%");
  });

  it("falls back to session cost, labelled as something else", () => {
    const html = show({
      ...NEVER,
      session_cost: {
        turns: 3,
        total_cost_usd: 0.0175,
        last_turn_cost_usd: 0.005,
        models: [
          { model: "claude-opus-5", cost_usd: 0.015, input_tokens: 1200, output_tokens: 300 },
        ],
        observed_at: OBSERVED,
      },
    });
    expect(html).toContain("Session cost — not plan usage");
    expect(html).toContain("$0.0175");
    expect(html).toContain("over 3 turns");
    expect(html).toContain("claude-opus-5");
  });

  it("says even the fallback is empty when no turn has finished", () => {
    expect(show(NEVER)).toContain("not even a session cost to show");
  });
});

describe("the panel with fresh figures", () => {
  it("draws one meter per reported window, and nothing for the others", () => {
    const html = show(FRESH);
    expect(html).toContain("5-hour limit");
    expect(html).toContain("Weekly, all models");
    // No sonnet row, no overage row: they were never reported.
    expect(html).not.toContain("Weekly, Sonnet");
    expect(html).not.toContain("Overage:");
    expect(html).toContain("42%");
    expect(html).toContain("86%");
    // 2 h from the observation, minus the 5 s the server had already counted:
    // the countdown runs on the server's clock, not the browser's.
    expect(html).toContain("Resets in 1h 59m");
    expect(html).toContain("Updated just now");
    expect(html).not.toContain("wb-usage-warning");
  });

  it("labels the bar that has earned a colour", () => {
    // DESIGN.md §7: the tint is emphasis, the words are the message.
    const html = show(FRESH);
    expect(html).toContain("is-warning");
    expect(html).toContain("Approaching limit");
  });

  it("shows a status and a dash when a window reported no figure", () => {
    const html = show({
      ...FRESH,
      buckets: [
        {
          kind: "five_hour",
          status: "allowed_warning",
          utilization: null,
          resets_at: null,
          observed_at: OBSERVED,
        },
      ],
    });
    expect(html).toContain(">—</span>");
    expect(html).toContain("Approaching limit");
    // The bar has nothing to draw, and the assistive text says so rather than
    // reporting a value of zero.
    expect(html).toContain('aria-valuetext="not reported"');
    expect(html).not.toContain("aria-valuenow");
  });
});

describe("the panel with a stale snapshot", () => {
  it("says the numbers are old, and why nothing can refresh them", () => {
    const html = show({ ...FRESH, age_s: STALE_AFTER_S + 600 });
    expect(html).toContain("wb-usage-warning");
    expect(html).toContain("old");
    expect(html).toContain("there is nothing to poll");
    // The figures are still shown — stale means old, not wrong.
    expect(html).toContain("42%");
  });

  it("ages on its own while the page sits open, with no event at all", () => {
    // Received fresh, held for twenty minutes: still stale, because staleness
    // is a fact about the figure and not about the fetch.
    const html = show({ ...FRESH, age_s: 5 }, (STALE_AFTER_S + 300) * 1000);
    expect(html).toContain("wb-usage-warning");
  });
});

describe("the status-bar reading", () => {
  it("shows nothing at all until a limit has been reported", () => {
    // §6.7: a quiet bar means nothing needs you. The QuickBar command is how
    // the capability stays reachable while there is nothing to say.
    expect(reading(null)).toBe("");
    expect(reading(NEVER)).toBe("");
  });

  it("reads the loudest window, with its words as well as its colour", () => {
    const html = reading(FRESH);
    expect(html).toContain("7d");
    expect(html).toContain("86%");
    expect(html).toContain("is-warning");
    expect(html).toContain("Approaching limit");
    // The tooltip carries the whole story, including how old it is.
    expect(html).toContain("Weekly, all models: 86% used");
    expect(html).toContain("Updated just now");
  });

  it("stays quiet while there is room", () => {
    const calm = { ...FRESH, buckets: [FRESH.buckets[0]] } as UsageSnapshot;
    const html = reading(calm);
    expect(html).toContain("is-normal");
    expect(html).not.toContain("is-warning");
    expect(html).not.toContain("is-critical");
    expect(html).toContain("42%");
  });

  it("marks a stale reading rather than passing it off as current", () => {
    const html = reading({ ...FRESH, age_s: STALE_AFTER_S + 60 });
    expect(html).toContain("is-stale");
    expect(html).toContain("ago");
  });
});
