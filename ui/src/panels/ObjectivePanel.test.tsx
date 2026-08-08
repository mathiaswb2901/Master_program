/**
 * The Objectives surface: the status badge and the card.
 *
 * Node-only static markup, like `ReviewPanel.test.tsx` — `ObjectiveBadge` and
 * `ObjectiveCard` take their status/objective as props, so they render to a
 * string with no store and no DOM. The neighbours that pull in dockview/registry
 * at import (`./Panes`, `../dock`) are stubbed. The card reuses the *real*
 * `RiskBadge` from `ReviewPanel`, so this also proves the evidence line renders
 * the same badge the Review panel does.
 *
 * The browser half — the panel opening, the goal set, the flip to `met` — is
 * `ui/e2e/objective.spec.ts`.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ObjectiveStatus, ValidationResult } from "../types";

vi.mock("./Panes", () => ({ revealPane: vi.fn() }));
vi.mock("../dock", () => ({ openPanel: vi.fn() }));

const { ObjectiveBadge, ObjectiveCard, objectiveTool, offTrackCount } = await import("./ObjectivePanel");
const { objectiveStatusVisual } = await import("../objectives");

const ALL: ObjectiveStatus[] = ["open", "met", "at-risk", "failing"];

describe("ObjectiveBadge", () => {
  it("renders each status with its own semantic colour and word", () => {
    for (const status of ALL) {
      const html = renderToStaticMarkup(<ObjectiveBadge status={status} />);
      const visual = objectiveStatusVisual(status);
      // The word is present (colour is never the only signal)…
      expect(html).toContain(visual.label);
      // …and the colour comes from that status's token, no other.
      expect(html).toContain(`var(${visual.token})`);
    }
  });

  it("gives four distinct colours across the four statuses", () => {
    const tokens = new Set(ALL.map((s) => objectiveStatusVisual(s).token));
    expect(tokens.size).toBe(4);
  });
});

describe("ObjectiveCard", () => {
  const goal = { statement: "Reconcile Åsen 2 revenue", acceptance: "within 0.1%" };

  it("shows the goal, the acceptance note, and no-evidence copy when open", () => {
    const html = renderToStaticMarkup(
      <ObjectiveCard objective={goal} status="open" evidence={null} />,
    );
    expect(html).toContain("Reconcile Åsen 2 revenue");
    expect(html).toContain("within 0.1%");
    expect(html).toContain("No evidence yet");
    expect(html).toContain("Open"); // the status badge word
  });

  it("renders the evidence result's own RiskBadge when met", () => {
    const evidence: ValidationResult = {
      validation_id: "val_1",
      subject: { kind: "objective", ref: "sess", label: "sess" },
      risk: "medium",
      evidence: [],
      summary: "off by 2 MWh",
      created_at: "2026-08-08T12:00:00Z",
      completed_at: "2026-08-08T12:00:00Z",
      truncated: null,
      approval: { approver: "you", timestamp: "2026-08-08T12:01:00Z", note: null },
    };
    const html = renderToStaticMarkup(
      <ObjectiveCard objective={goal} status="met" evidence={evidence} />,
    );
    expect(html).toContain("Met"); // the objective status
    expect(html).toContain("off by 2 MWh"); // the evidence summary
    // The Review panel's own risk badge, on the evidence's risk — reuse, not a
    // second badge: "Medium risk" is `RiskBadge`'s label for `medium`.
    expect(html).toContain("Medium risk");
  });

  it("renders two cards independently — no state bleeds across (plural)", () => {
    const a = renderToStaticMarkup(
      <ObjectiveCard objective={{ statement: "goal A", acceptance: null }} status="met" evidence={null} />,
    );
    const b = renderToStaticMarkup(
      <ObjectiveCard objective={{ statement: "goal B", acceptance: null }} status="failing" evidence={null} />,
    );
    expect(a).toContain("goal A");
    expect(a).toContain("Met");
    expect(a).not.toContain("goal B");
    expect(b).toContain("goal B");
    expect(b).toContain("Failing");
    expect(b).not.toContain("goal A");
  });
});

describe("offTrackCount", () => {
  it("counts only at-risk / failing objectives, hides at zero", () => {
    const results: ValidationResult[] = [
      {
        validation_id: "v1",
        subject: { kind: "objective", ref: "a", label: "a" },
        risk: "high",
        evidence: [],
        summary: "s",
        created_at: "2026-08-08T12:00:00Z",
        completed_at: null,
        truncated: null,
        approval: null,
      },
    ];
    const sessions = [
      { id: "a", objective: { statement: "goal A", acceptance: null } },
      { id: "b", objective: { statement: "goal B", acceptance: null } },
    ];
    // a is failing (unapproved high); b has no evidence → open. So one off track.
    expect(offTrackCount(sessions, {}, results)).toBe(1);
    // Nothing off track → zero (the reading hides).
    expect(offTrackCount([{ id: "b", objective: sessions[1].objective }], {}, [])).toBe(0);
  });

  it("honours a cleared objective (map null) over a stale session snapshot", () => {
    const results: ValidationResult[] = [
      {
        validation_id: "v1",
        subject: { kind: "objective", ref: "a", label: "a" },
        risk: "high",
        evidence: [],
        summary: "s",
        created_at: "2026-08-08T12:00:00Z",
        completed_at: null,
        truncated: null,
        approval: null,
      },
    ];
    // Session a still carries its old goal in the last list snapshot, but the
    // objective store has it cleared (objectives.a === null, not undefined). The
    // clear must win — a `??` chain would fall back to the stale goal and keep
    // counting a failing objective that no longer exists.
    const sessions = [{ id: "a", objective: { statement: "goal A", acceptance: null } }];
    expect(offTrackCount(sessions, { a: null }, results)).toBe(0);
    // Sanity: without the clear (map empty), the stale goal is still counted.
    expect(offTrackCount(sessions, {}, results)).toBe(1);
  });
});

describe("the tool descriptor", () => {
  it("is a plural, on-demand panel with a command and a status reading", () => {
    expect(objectiveTool.id).toBe("objective");
    expect(objectiveTool.panel?.singleton).toBe(false);
    expect(objectiveTool.panel?.openByDefault).toBe(false);
    expect(objectiveTool.commands?.some((c) => c.id === "objective.open")).toBe(true);
    expect(objectiveTool.statusContributions?.length).toBeGreaterThan(0);
  });
});
