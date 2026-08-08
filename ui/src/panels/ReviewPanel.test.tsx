/**
 * The Review surface: the badge, the evidence gallery, the approval gate, and
 * the plural pane picker.
 *
 * Node-only static markup, like `VisualPanel.test.tsx` — the pure components
 * (badge, pill, gallery, view, gate) take their result as a prop, so they render
 * to a string with no store and no DOM. The two neighbours that pull in
 * dockview/Monaco at import (`./Panes`, `../dock`) are stubbed; the module under
 * test is the real one, and the real `validation.ts` store backs the picker.
 *
 * The browser half — a real result driven in through `POST /api/validation/run`,
 * the panel opening, the approve click — is `ui/e2e/review.spec.ts`.
 */

import type { IDockviewPanelProps } from "dockview";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CheckOutcome, EvidenceItem, RiskLevel, ValidationResult } from "../types";

// `./Panes` pulls in dockview's runtime; `../dock` the whole registry. Neither
// is exercised by static rendering — the picker's `key()` returns a string and
// the command's `run` is never fired here.
vi.mock("./Panes", () => ({ revealPane: vi.fn() }));
vi.mock("../dock", () => ({ openPanel: vi.fn() }));

// The store, mocked to read a mutable state object — the `AgentPanel.test.tsx`
// pattern. This is what lets a *mounted* `ReviewPanel` resolve its pane id
// against seeded results: node-only `renderToStaticMarkup` reads zustand's
// *initial* snapshot through the real hook, so a real store could never carry
// mutated state into an SSR render. The pure rules (`riskVisual`,
// `awaitingApproval`, …) stay real via `importActual`; only the hook is swapped.
let vstate: { results: Record<string, ValidationResult>; hydrated: boolean } = {
  results: {},
  hydrated: false,
};
const approveMock = vi.fn();
const initMock = vi.fn();

vi.mock("../validation", async (importActual) => {
  const actual = await importActual<typeof import("../validation")>();
  const snapshot = (): Record<string, unknown> => ({ ...vstate, approve: approveMock, init: initMock });
  const useValidationStore = Object.assign((select: (s: Record<string, unknown>) => unknown) => select(snapshot()), {
    getState: snapshot,
    setState: (patch: Partial<typeof vstate>) => {
      vstate = { ...vstate, ...patch };
    },
  });
  return {
    ...actual,
    useValidationStore,
    resetValidationStoreForTests: () => {
      vstate = { results: {}, hydrated: false };
    },
  };
});

const { resetValidationStoreForTests, useValidationStore } = await import("../validation");

const {
  ApprovalGate,
  EvidenceGallery,
  OutcomePill,
  ReviewPanel,
  ReviewView,
  RiskBadge,
  reviewTool,
  worstAwaiting,
} = await import("./ReviewPanel");

/** Seed the mocked store with a set of results and mark it hydrated (the
 * post-load steady state). Tests that need the pre-load race set `hydrated`
 * themselves. */
function seed(results: ValidationResult[], hydrated = true): void {
  useValidationStore.setState({
    results: Object.fromEntries(results.map((r) => [r.validation_id, r])),
    hydrated,
  });
}

/** A minimal dockview panel props with just the `api` the panel reads — the
 * pane's own id (its `review#<validation_id>` string) and a `close`. */
function panelProps(paneId: string): IDockviewPanelProps {
  return { api: { id: paneId, close: vi.fn() } } as unknown as IDockviewPanelProps;
}

/** Render one real `ReviewPanel` instance bound to a pane id, against whatever
 * the shared `useValidationStore` currently holds — the true wiring the trivial
 * two-`ReviewView` test never exercised. */
const panel = (paneId: string): string => html(<ReviewPanel {...panelProps(paneId)} />);

const ALL_RISKS: RiskLevel[] = ["pass", "low", "medium", "high", "blocked"];

function evidence(patch: Partial<EvidenceItem> = {}): EvidenceItem {
  return { kind: "gate", label: "ruff", outcome: "pass", detail: "0 findings", payload_ref: null, ...patch };
}

function result(id: string, patch: Partial<ValidationResult> = {}): ValidationResult {
  return {
    validation_id: id,
    subject: { kind: "session_output", ref: "sess-1", label: `subject ${id}` },
    risk: "pass",
    evidence: [],
    summary: "1 pass",
    created_at: "2026-08-08T10:00:00Z",
    completed_at: "2026-08-08T10:00:01Z",
    truncated: null,
    approval: null,
    ...patch,
  };
}

const html = (node: JSX.Element): string => renderToStaticMarkup(node);
const noop = (): void => undefined;

const view = (result: ValidationResult, stale: string | null = null): string =>
  html(
    <ReviewView
      result={result}
      note=""
      pending={false}
      stale={stale}
      onNote={noop}
      onApprove={noop}
    />,
  );

afterEach(() => {
  resetValidationStoreForTests();
  vi.clearAllMocks();
});

// ---- the risk badge, every level, with its label and aria-label ------------

describe("RiskBadge", () => {
  it("renders each risk level with the right token and its word", () => {
    const expected: Record<RiskLevel, { token: string; label: string }> = {
      pass: { token: "--success", label: "Pass" },
      low: { token: "--info", label: "Low risk" },
      medium: { token: "--warn", label: "Medium risk" },
      high: { token: "--error", label: "High risk" },
      blocked: { token: "--agent-idle", label: "Blocked" },
    };
    for (const risk of ALL_RISKS) {
      const markup = html(<RiskBadge risk={risk} />);
      expect(markup).toContain(`var(${expected[risk].token})`);
      expect(markup).toContain(expected[risk].label);
      // Not the amber accent — §2.4 spends that elsewhere.
      expect(markup).not.toContain("var(--accent");
    }
  });

  it("the dot-only variant always carries an aria-label (colour is never alone)", () => {
    const markup = html(<RiskBadge risk="high" dotOnly />);
    expect(markup).toContain('aria-label="Validation risk: High risk"');
    expect(markup).toContain("wb-pill-dotonly");
  });
});

describe("OutcomePill", () => {
  it("renders each outcome with a colour and a label", () => {
    const cases: Record<CheckOutcome, string> = {
      pass: "Pass",
      warn: "Warn",
      fail: "Fail",
      skipped: "Skipped",
    };
    for (const [outcome, label] of Object.entries(cases)) {
      expect(html(<OutcomePill outcome={outcome as CheckOutcome} />)).toContain(label);
    }
  });
});

// ---- the evidence gallery: a row per item, and the explicit "none" ---------

describe("EvidenceGallery", () => {
  it("renders one row per EvidenceItem, each with its outcome pill and detail", () => {
    const r = result("v1", {
      risk: "high",
      evidence: [
        evidence({ label: "ruff", outcome: "pass", detail: "0 findings" }),
        evidence({ kind: "numeric", label: "reconciliation", outcome: "fail", detail: "3 of 40 mismatch" }),
      ],
    });
    const markup = html(<EvidenceGallery result={r} />);
    const rows = markup.match(/class="wb-evidence"/g) ?? [];
    expect(rows).toHaveLength(2);
    expect(markup).toContain("ruff");
    expect(markup).toContain("reconciliation");
    expect(markup).toContain("3 of 40 mismatch");
    // The outcomes render as pills — a pass and a fail.
    expect(markup).toContain("Pass");
    expect(markup).toContain("Fail");
    // The numeric detail is in tabular figures (the DESIGN numeric rule).
    expect(markup).toContain("u-tabular");
  });

  it("an empty gallery says so — never blankness (AXI shape 2)", () => {
    const markup = html(<EvidenceGallery result={result("v1", { risk: "blocked" })} />);
    expect(markup).toContain("wb-review-none");
    expect(markup).toContain("No evidence");
  });

  it("a truncated evidence list names the cut and how to widen it (AXI shape 1)", () => {
    const r = result("v1", {
      evidence: [evidence()],
      truncated: { shown: 1, total: 5, detail: "showing 1 of 5; run fewer checks to see the rest" },
    });
    expect(html(<EvidenceGallery result={r} />)).toContain("showing 1 of 5");
  });

  it("an item with a payload_ref offers an expand that names the deferred payload", () => {
    const r = result("v1", { evidence: [evidence({ payload_ref: "numeric_abc", kind: "numeric" })] });
    const markup = html(<EvidenceGallery result={r} />);
    expect(markup).toContain("numeric_abc");
    expect(markup).toContain("deferred");
  });
});

// ---- the whole review, and the approval gate -------------------------------

describe("ReviewView", () => {
  it("renders the badge, the subject and the summary", () => {
    const markup = view(result("v1", { risk: "medium", subject: { kind: "session_output", ref: "s", label: "Åsen dispatch" } }));
    expect(markup).toContain("Medium risk");
    expect(markup).toContain("Åsen dispatch");
    expect(markup).toContain("1 pass");
    expect(markup).toContain('data-validation="v1"');
  });

  it("a medium-or-worse result awaiting a human shows Awaiting approval + Approve", () => {
    const markup = view(result("v1", { risk: "high" }));
    expect(markup).toContain("Awaiting approval");
    expect(markup).toContain(">Approve<");
  });

  it("a pass result shows no approval affordance — none is owed", () => {
    const markup = view(result("v1", { risk: "pass" }));
    expect(markup).not.toContain("Awaiting approval");
    expect(markup).not.toContain(">Approve<");
  });

  it("an approved result reflects who and when, and offers no button", () => {
    const markup = view(
      result("v1", {
        risk: "high",
        approval: { approver: "you", timestamp: "2026-08-08T10:00:05Z", note: "cleared" },
      }),
    );
    expect(markup).toContain("Approved by you");
    expect(markup).toContain("cleared");
    expect(markup).not.toContain(">Approve<");
  });

  it("a 404 on approve surfaces as 'no longer current', not a success", () => {
    const markup = view(result("v1", { risk: "high" }), "This result is no longer current — it was superseded or evicted. Re-run the validation.");
    expect(markup).toContain("no longer current");
    expect(markup).toContain('role="alert"');
  });
});

describe("ApprovalGate", () => {
  it("renders nothing for a result that owes no approval", () => {
    const markup = html(
      <ApprovalGate result={result("v1", { risk: "low" })} note="" pending={false} stale={null} onNote={noop} onApprove={noop} />,
    );
    expect(markup).toBe("");
  });
});

// ---- plural: two panes are independent, and the picker lists results -------

describe("worstAwaiting", () => {
  it("is the most severe result still awaiting review, or null when none is", () => {
    expect(worstAwaiting([])).toBeNull();
    expect(worstAwaiting([result("a", { risk: "pass" })])).toBeNull();
    expect(
      worstAwaiting([result("a", { risk: "medium" }), result("b", { risk: "blocked" })]),
    ).toBe("blocked");
  });
});

describe("two Review panes are independent (the plural contract)", () => {
  it("two ReviewPanel instances resolve two different ids against the shared store", () => {
    // Two whole panes, not two calls to a pure view: each carries its own
    // `review#<validation_id>` pane id, and each must resolve *its* id through
    // `paneInstance` against the one `useValidationStore` map. A pane-id-parsing
    // or stale-closure regression that made both resolve to the same result
    // would fail this and pass the old two-`ReviewView` test.
    seed([
      result("val_aaaa", { risk: "high", subject: { kind: "session_output", ref: "s1", label: "output A" } }),
      result("val_bbbb", { risk: "low", subject: { kind: "session_output", ref: "s2", label: "output B" } }),
    ]);

    const a = panel("review#val_aaaa");
    const b = panel("review#val_bbbb");

    expect(a).toContain('data-validation="val_aaaa"');
    expect(a).toContain("output A");
    expect(a).toContain("High risk");
    expect(b).toContain('data-validation="val_bbbb"');
    expect(b).toContain("output B");
    expect(b).toContain("Low risk");
    // Nothing bleeds from one pane into the other — the whole point of binding a
    // pane to an id rather than to "the review panel".
    expect(a).not.toContain("output B");
    expect(a).not.toContain("Low risk");
    expect(b).not.toContain("output A");
    expect(b).not.toContain("High risk");
  });

  it("the bare pane (no id) is the index of every held result", () => {
    seed([
      result("val_aaaa", { subject: { kind: "session_output", ref: "s1", label: "output A" } }),
      result("val_bbbb", { subject: { kind: "session_output", ref: "s2", label: "output B" } }),
    ]);
    const index = panel("review");
    expect(index).toContain("wb-review-index");
    expect(index).toContain("output A");
    expect(index).toContain("output B");
  });
});

// ---- a restored pane is vetted before it is believed (hydration gate) -------

describe("a restored pane bound to an absent id", () => {
  it("waits (never a tombstone) until the store has answered", () => {
    // Fresh store: hydrated is false, the map empty — the exact cold-launch race
    // where the layout restores a pane before `GET /api/validation` resolves. A
    // tombstone here would flash 're-run it' over a result that is in fact live.
    const before = panel("review#val_still_held");
    expect(before).toContain("wb-review-loading");
    expect(before).not.toContain("wb-review-tombstone");
    expect(before).not.toContain("no longer loaded");
  });

  it("falls through to the tombstone only once the store has answered and the id is gone", () => {
    // The store answered (a refresh settled) and the id is genuinely absent —
    // now, and only now, the tombstone is the honest view.
    useValidationStore.setState({ hydrated: true, results: {} });
    const after = panel("review#val_evicted");
    expect(after).toContain("wb-review-tombstone");
    expect(after).toContain("no longer loaded");
    expect(after).not.toContain("wb-review-loading");
  });
});

describe("reviewTool registration", () => {
  const instances = reviewTool.panel?.instances;

  it("is a plural, on-demand panel with a status reading and a command", () => {
    expect(reviewTool.id).toBe("review");
    expect(reviewTool.panel?.singleton).toBe(false);
    expect(reviewTool.panel?.openByDefault).toBe(false);
    expect(reviewTool.statusContributions).toHaveLength(1);
    expect(reviewTool.commands?.[0]?.id).toBe("review.open");
    // No default chord — bindable from shortcuts.md the day it registers.
    expect(reviewTool.shortcuts).toBeUndefined();
  });

  it("options() lists a row per held result, each keyed by its validation_id", () => {
    useValidationStore.setState({
      results: {
        v1: result("v1", { subject: { kind: "session_output", ref: "s1", label: "first" } }),
        v2: result("v2", { subject: { kind: "session_output", ref: "s2", label: "second" }, created_at: "2026-08-08T11:00:00Z" }),
      },
    });
    const rows = instances?.options?.() ?? [];
    expect(rows.map((row) => row.key())).toEqual(["v1", "v2"]);
    expect(rows[0]).toMatchObject({ id: "review.v1", title: "first", category: "Validations" });
  });

  it("titleFor() names a live result by its subject, and falls back to the id", () => {
    useValidationStore.setState({ results: { v1: result("v1", { subject: { kind: "file", ref: "wb.xlsx", label: "wb.xlsx" } }) } });
    expect(instances?.titleFor?.("v1")).toBe("wb.xlsx");
    // A restored pane whose result the restart forgot still reads as its id.
    expect(instances?.titleFor?.("gone")).toBe("gone");
  });
});
