/**
 * The Specs panel's receipt: **the list of files an approval covers is on screen
 * before the click, not after it.**
 *
 * Node-only static markup, the `ReviewPanel.test.tsx` pattern — the store is
 * swapped for a mutable snapshot so a `renderToStaticMarkup` pass can read
 * seeded rows, and `../dock` is stubbed because it pulls the whole registry in
 * at import.
 *
 * The claim under test is the one the panel's own copy makes. "This spec,
 * running exactly this code, on this machine, until either changes" is only
 * worth saying next to a list a person can check, and the person doing the
 * checking is looking at an **unapproved** row — `approval` is still null there,
 * so `pending_covered` is the only place those paths can come from. A panel that
 * shows the coverage only once it has been approved is asking for a signature on
 * a document it hands over afterwards.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CoveredSource, SpecState } from "../types";

// The registry behind `openPanel`; nothing here fires a command.
vi.mock("../dock", () => ({ openPanel: vi.fn() }));

// The store, mocked to read a mutable object — a real zustand instance would
// only ever hand an SSR render its *initial* snapshot. The pure rules
// (`orderSpecs`, `statusVisual`, …) stay real via `importActual`.
let sstate: { specs: Record<string, SpecState>; detail: string; hydrated: boolean; error: string | null } =
  { specs: {}, detail: "", hydrated: true, error: null };

vi.mock("../reconcileSpec", async (importActual) => {
  const actual = await importActual<typeof import("../reconcileSpec")>();
  const snapshot = (): Record<string, unknown> => ({
    ...sstate,
    approve: vi.fn(),
    revoke: vi.fn(),
    run: vi.fn(),
    init: vi.fn(),
    refresh: vi.fn(),
  });
  const useSpecStore = Object.assign(
    (select: (state: Record<string, unknown>) => unknown) => select(snapshot()),
    { getState: snapshot },
  );
  return { ...actual, useSpecStore };
});

const { SpecPanel } = await import("./SpecPanel");

function covered(path: string, origin: CoveredSource["origin"]): CoveredSource {
  return { path, digest: "0f1e2d", origin };
}

const PENDING: CoveredSource[] = [
  covered(".workbench/reconcile/dispatch.toml", "spec"),
  covered("se3/reporting.py", "callable"),
];

function spec(patch: Partial<SpecState> = {}): SpecState {
  return {
    name: "dispatch",
    path: ".workbench/reconcile/dispatch.toml",
    workbook: "models/budget.xlsx",
    status: "unapproved",
    digest: "as-rendered",
    checks: 1,
    approval: null,
    pending_covered: [],
    last_run: null,
    detail: "Not approved.",
    ...patch,
  };
}

function render(row: SpecState): string {
  sstate = { specs: { [row.name]: row }, detail: "1 spec(s): 1 unapproved.", hydrated: true, error: null };
  return renderToStaticMarkup(<SpecPanel />);
}

afterEach(() => {
  vi.clearAllMocks();
  sstate = { specs: {}, detail: "", hydrated: true, error: null };
});

describe("what an Approve button is asking for", () => {
  it("names every file the approval would cover, beside the button", () => {
    const html = render(spec({ pending_covered: PENDING }));
    expect(html).toContain("se3/reporting.py");
    expect(html).toContain(".workbench/reconcile/dispatch.toml");
    // …and the button really is on the same card, so this is a decision taken
    // next to its receipt rather than a list somewhere else in the app.
    expect(html).toContain(">Approve<");
    expect(html).toContain("Files this approval would cover");
  });

  it("a stale row says what approving *again* would cover", () => {
    const html = render(
      spec({
        status: "stale",
        pending_covered: PENDING,
        approval: {
          name: "dispatch",
          digest: "old",
          approver: "you",
          approved_at: "2026-08-09T09:00:00",
          covered: [covered("se3/reporting.py", "callable")],
        },
      }),
    );
    expect(html).toContain("Files approving again would cover");
    expect(html).toContain("Files this approval covers");
    expect(html).toContain(">Approve again<");
  });

  it("an approved row shows the approval's own receipt and does not repeat it", () => {
    const html = render(
      spec({
        status: "approved",
        pending_covered: [],
        approval: {
          name: "dispatch",
          digest: "current",
          approver: "you",
          approved_at: "2026-08-09T09:00:00",
          covered: PENDING,
        },
      }),
    );
    expect(html).toContain("Files this approval covers");
    expect(html).not.toContain("would cover");
    expect(html).toContain("se3/reporting.py");
  });
});
