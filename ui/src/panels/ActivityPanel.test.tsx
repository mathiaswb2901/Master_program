/**
 * The fleet panel at zero, one and four sessions.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design —
 * `vitest.config.ts`). Four concurrent sessions is the state the whole feature
 * is judged on and the one a browser journey cannot reliably stage — four live
 * agents all mid-tool-call at the same instant — so it is asserted here, on the
 * rendering, and the E2E journey covers what a real turn can reach.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ActivityEntry, ActivitySnapshot, SessionActivity, SessionState } from "../types";

import { ActivityBody, ActivityReading, EMPTY_STATE_ICON, SessionCard } from "./ActivityPanel";

// Three modules this panel reaches through and never exercises. `openPanel`
// pulls in the whole registry (and with it Monaco's browser bundle); the store
// reads `document` at import; and `statusVisual` — the §2.6 vocabulary this
// panel deliberately shares with the session rows rather than restating —
// lives next to the plan card, which builds a draft off the real store at
// module scope. The mapping under test is the real one; only its neighbours
// are stubbed.
vi.mock("../dock", () => ({ openPanel: () => undefined }));
vi.mock("./PlanCard", () => ({ PlanCard: () => null }));

const sessionStates: Record<string, SessionState> = { four: "needs_attention" };

vi.mock("../store", () => ({
  useStore: Object.assign(
    (select: (state: unknown) => unknown) =>
      select({ sessionStates, sessionFlags: {}, tree: null }),
    { getState: () => ({ openFile: () => undefined, openSessionById: () => undefined }) },
  ),
}));

const T = 1_700_000_000;
const NOW_MS = T * 1000;

function entry(id: string, patch: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    entry_id: id,
    tool: "Edit",
    summary: `Edit: src/${id}.py`,
    target: `src/${id}.py`,
    started_at: T,
    settled_at: null,
    ok: null,
    ...patch,
  };
}

function session(
  id: string,
  entries: ActivityEntry[],
  patch: Partial<SessionActivity> = {},
): SessionActivity {
  return {
    session_id: id,
    folder: "",
    title: `session ${id}`,
    kind: "chat",
    entries,
    dropped: 0,
    active_at: T,
    ...patch,
  };
}

function snapshot(sessions: SessionActivity[], patch: Partial<ActivitySnapshot> = {}): ActivitySnapshot {
  return {
    sessions,
    max_entries_per_session: 8,
    max_sessions: 16,
    dropped_sessions: 0,
    ...patch,
  };
}

const show = (value: ActivitySnapshot | null): string =>
  renderToStaticMarkup(<ActivityBody snapshot={value} now={NOW_MS} />);

const reading = (value: ActivitySnapshot | null): string =>
  renderToStaticMarkup(<ActivityReading snapshot={value} />);

describe("an empty fleet", () => {
  it("says nothing is running, and why that is the normal state", () => {
    const html = show(snapshot([]));
    expect(html).toContain("No agent sessions running");
    expect(html).toContain("No sessions");
    // The claim that makes this panel worth opening at all.
    expect(html).toContain("including sessions you have never opened a chat for");
    expect(html).toContain("empty whenever the fleet is");
    // Deliberate, not broken: no half-rendered row, no zeroed anything.
    expect(html).not.toContain("wb-activity-session");
  });

  it("draws its icon at the size DESIGN.md §6.10 fixes, not the tab's", () => {
    // The glyph is shared with the tab and the status bar, which want 14px. An
    // empty state is specified at 32px / 1.5 stroke, and nothing in the
    // stylesheet scales an SVG — so the size has to come from the element.
    expect(EMPTY_STATE_ICON).toEqual({ size: 32, strokeWidth: 1.5 });
    const html = show(snapshot([]));
    const icon = html.slice(html.indexOf("wb-activity-empty-icon"));
    expect(icon).toContain('width="32"');
    expect(icon).toContain('height="32"');
    expect(icon).toContain('stroke-width="1.5"');
    // The other two surfaces keep the size that suits the text beside them.
    expect(reading(snapshot([session("one", [entry("a")])]))).toContain('width="14"');
  });

  it("distinguishes 'not loaded yet' from 'nothing running'", () => {
    expect(show(null)).toContain("Reading activity…");
    expect(show(null)).not.toContain("No agent sessions running");
  });
});

describe("one session", () => {
  it("shows what it is doing now and what it just did", () => {
    const html = show(
      snapshot([
        session("one", [
          entry("now", { summary: "Edit: src/prices.py", target: "src/prices.py" }),
          entry("then", {
            summary: "Read: src/model.py",
            target: "src/model.py",
            settled_at: T - 1,
            ok: true,
          }),
        ]),
      ]),
    );
    expect(html).toContain(">Now</span>");
    expect(html).toContain("Edit: src/prices.py");
    expect(html).toContain(">Just did</span>");
    expect(html).toContain("Read: src/model.py");
    expect(html).toContain(">Done</span>");
    expect(html).toContain("1 of 1 working");
    // The folder line names the root rather than rendering an empty string.
    expect(html).toContain("workspace root");
  });

  it("makes a file target a real button, and leaves a commandless call as text", () => {
    const html = show(
      snapshot([
        session("one", [
          entry("bash", {
            tool: "Bash",
            summary: "Bash: uv run pytest",
            target: null,
          }),
        ]),
      ]),
    );
    expect(html).toContain("Bash: uv run pytest");
    // Nothing to open, so nothing that looks openable.
    expect(html).not.toContain("wb-activity-target");
    expect(show(snapshot([session("one", [entry("a")])]))).toContain(
      'title="Open src/a.py"',
    );
  });

  it("reads as quiet, not as broken, when the session has run nothing", () => {
    const html = show(snapshot([session("idle", [])]));
    expect(html).toContain("Nothing running");
    expect(html).toContain("0 of 1 working");
    expect(html).not.toContain("Just did");
  });

  it("says a failure in words as well as in colour", () => {
    const html = show(
      snapshot([session("one", [entry("a", { settled_at: T, ok: false })])]),
    );
    expect(html).toContain(">Failed</span>");
    expect(html).toContain("--agent-error");
  });

  it("counts what its window dropped rather than hiding the gap", () => {
    const html = show(
      snapshot([
        session("one", [entry("a"), entry("b", { settled_at: T, ok: true }), entry("c")], {
          dropped: 37,
        }),
      ]),
    );
    expect(html).toContain("37 dropped");
    expect(html).toContain("Earlier in this session (1)");
  });
});

describe("four sessions at once", () => {
  const FLEET = snapshot([
    session("one", [entry("a", { summary: "Edit: se3/bid.py", target: "se3/bid.py" })], {
      active_at: T,
      title: "fix the gate closure",
    }),
    session("two", [entry("b", { tool: "Bash", summary: "Bash: uv run pytest", target: null })], {
      active_at: T - 1,
      folder: "se3",
      title: "run the suite",
    }),
    session("three", [entry("c", { settled_at: T - 2, ok: true })], {
      active_at: T - 2,
      title: "read the forecast",
    }),
    session("four", [], { active_at: T - 3, title: "waiting on you" }),
  ]);

  it("gives every session its own row, in fleet order", () => {
    const html = show(FLEET);
    expect(html.match(/wb-activity-session"/g)).toHaveLength(4);
    const order = ["fix the gate closure", "run the suite", "read the forecast", "waiting on you"];
    const positions = order.map((title) => html.indexOf(title));
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    expect(positions.every((index) => index > 0)).toBe(true);
  });

  it("reads each row at its own rhythm — two working, one done, one idle", () => {
    const html = show(FLEET);
    expect(html).toContain("2 of 4 working");
    expect(html).toContain("Edit: se3/bid.py");
    expect(html).toContain("Bash: uv run pytest");
    expect(html).toContain("Nothing running");
    expect(html).toContain("Just did");
  });

  it("uses the §2.6 status vocabulary, including one blocked on the user", () => {
    const html = show(FLEET);
    // From `statusVisual`, not from a second colour table in this module.
    expect(html).toContain("--agent-working");
    expect(html).toContain("--agent-attention");
    expect(html).toContain('aria-label="Needs attention"');
    expect(html).toContain('aria-label="Working"');
  });

  it("states the caps it is showing under", () => {
    expect(show(FLEET)).toContain("The last 8 tool calls per session, up to 16 sessions");
    expect(show(snapshot(FLEET.sessions, { dropped_sessions: 3 }))).toContain(
      "3 older sessions have dropped out of it",
    );
  });
});

describe("what a frame costs to render", () => {
  it("puts a memo boundary on the row, so a busy session is one card's work", () => {
    // Every `session_activity` frame replaces the whole snapshot object, so
    // `ActivityBody` re-runs on each of them — up to four times a second while
    // the fleet is busy. The *rows* must not: with four agents working, a burst
    // in one session's tool calls would otherwise re-render the other three
    // cards on every frame, for data that did not move. That is the re-render
    // storm the server's 250 ms coalescing exists to prevent, one layer later.
    //
    // Two halves, and this is the second. `mergeSessions` keeping the identity
    // of rows the frame did not carry is pinned in `activity.test.ts`; this
    // boundary is what turns that stable reference into a skipped render.
    //
    // Asserted on the element type rather than by counting renders: the suite
    // is node-only by design (`vitest.config.ts`) and `renderToStaticMarkup`
    // never bails out of a memo — server rendering has nothing to reuse — so a
    // render count here would pass whether or not the boundary existed.
    expect(SessionCard.$$typeof).toBe(Symbol.for("react.memo"));
  });
});

describe("the status-bar reading", () => {
  it("shows nothing while the fleet is idle — a quiet bar means nothing needs you", () => {
    expect(reading(null)).toBe("");
    expect(reading(snapshot([]))).toBe("");
    expect(reading(snapshot([session("idle", [])]))).toBe("");
    expect(reading(snapshot([session("done", [entry("a", { settled_at: T, ok: true })])]))).toBe("");
  });

  it("counts the sessions running a tool and names them in its tooltip", () => {
    const html = reading(
      snapshot([
        session("one", [entry("a", { summary: "Edit: se3/bid.py" })], { title: "bid work" }),
        session("two", [entry("b", { summary: "Grep: gate closure" })], { title: "search" }),
        session("three", []),
      ]),
    );
    expect(html).toContain(">2</span>");
    expect(html).toContain("2 sessions are running a tool");
    expect(html).toContain("bid work: Edit: se3/bid.py");
    expect(html).toContain("Open live agent activity");
  });

  it("says how many calls are in flight when one session holds several", () => {
    const html = reading(snapshot([session("one", [entry("a"), entry("b")])]));
    expect(html).toContain("1 session is running a tool (2 calls)");
  });
});
