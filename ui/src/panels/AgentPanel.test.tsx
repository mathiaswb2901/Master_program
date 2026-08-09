/**
 * What the Agent panel *shows* — the pane's binding, and the picker's chrome.
 *
 * Two halves, both rendered rather than asserted about as source text:
 *
 *  1. the three states a session pane can be in when its binding is judged
 *     (M5 item 15) — a pane is a *claim* about a session, vetted before it is
 *     believed;
 *  2. the picker's own chrome (ANVIL V5) — the concurrency ceiling, the empty
 *     list, and the row that says what a session is *doing* in words. Each of
 *     those is a conditional, and a conditional nothing renders is a conditional
 *     you can invert without a gate noticing.
 *
 * Static markup rather than a DOM stack (the suite is node-only — the effects
 * that open the socket never run here, which is the point: this asserts what a
 * pane *shows* before it decides to connect). The store is the real selector run
 * against a hand-built state, exactly as `ActivityPanel.test.tsx` does it, and
 * `statusVisual`/`dotClass` are the **real** ones: a row's words come from the
 * §2.6 vocabulary table, so a test that stubbed it would be asserting its own
 * fixture rather than what a working session actually says.
 */

import type { IDockviewPanelProps } from "dockview";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { SessionFlags } from "../store";
import type { FolderSessions, SessionInfo, SessionLimits, SessionState } from "../types";
import type * as ChatModule from "./Chat";

// Neighbours this pane reaches through but does not exercise here. `../dock` and
// `./Panes` pull in the dock runtime and the whole registry; `./Chat`'s *body*
// is what would mount in the live case — a marker stands in for it so "the chat
// mounted" is observable without Monaco, while the module's pure status helpers
// stay real; `./PlanCard` builds a plan draft off the real store at module scope
// (the Agent descriptor spreads its commands).
vi.mock("../dock", () => ({ dockApiHandle: () => null, focusPanel: () => undefined }));
vi.mock("./Panes", () => ({ revealPane: () => undefined }));
vi.mock("./Chat", async () => ({
  ...(await vi.importActual<typeof ChatModule>("./Chat")),
  Chat: ({ sessionId }: { sessionId: string }) => <div data-chat={sessionId}>chat</div>,
  TranscriptView: () => null,
}));
vi.mock("./PlanCard", () => ({
  planCommands: [],
  planShortcuts: {},
  PlanCard: () => null,
}));

interface StoreState {
  folders: FolderSessions[];
  detachedSessions: Record<string, string>;
  activeSessionId: string | null;
  transcriptView: null;
  sessionStates: Record<string, SessionState>;
  sessionFlags: Record<string, SessionFlags>;
  sessionLimits: SessionLimits | null;
}

/** A whole store, so a test names only the slice it is about. */
function storeState(over: Partial<StoreState> = {}): StoreState {
  return {
    folders: [],
    detachedSessions: {},
    activeSessionId: null,
    transcriptView: null,
    sessionStates: {},
    sessionFlags: {},
    sessionLimits: null,
    ...over,
  };
}

let state: StoreState = storeState();
const reattachSession = vi.fn();

vi.mock("../store", () => ({
  useStore: Object.assign((select: (s: unknown) => unknown) => select(state), {
    getState: () => ({ reattachSession }),
  }),
}));

// Imported after the mocks are in place.
const { AgentPanel } = await import("./AgentPanel");

function session(over: Partial<SessionInfo>): SessionInfo {
  return {
    session_id: "sess1",
    folder: "",
    state: "idle",
    live: true,
    title: "A conversation",
    updated_at: 1,
    kind: "chat",
    ...over,
  };
}

/** One live session in the workspace root — the picker's ordinary case. */
function oneLive(over: Partial<SessionInfo> = {}): FolderSessions[] {
  return [{ folder: "", sessions: [session({ live: true, ...over })] }];
}

function render(sessionId: string): string {
  const props = { api: { id: `agent#${sessionId}` } } as unknown as IDockviewPanelProps;
  return renderToStaticMarkup(<AgentPanel {...props} />);
}

/** The unbound pane: the session list plus the chat area. */
function renderBrowser(): string {
  const props = { api: { id: "agent" } } as unknown as IDockviewPanelProps;
  return renderToStaticMarkup(<AgentPanel {...props} />);
}

/** The *New session* control alone — the chat area's empty state quotes the same
 * command, so a page-wide `toContain` would pass on the wrong element. */
function newSessionButton(html: string): string {
  return /<button[^>]*>New session<\/button>/.exec(html)?.[0] ?? "";
}

/** One picker row alone, for the same reason: the pane holds several dots. */
function sessionRow(html: string): string {
  return /<button[^>]*class="wb-session-row[^"]*"[\s\S]*?<\/button>/.exec(html)?.[0] ?? "";
}

describe("a session pane, judged against its binding", () => {
  it("streams when the session is live and not detached", () => {
    state = storeState({ folders: oneLive() });
    const html = render("sess1");
    expect(html).toContain('data-chat="sess1"');
    expect(html).not.toContain("detached");
    expect(html).not.toContain("not running any more");
  });

  it("offers Resume when the session is live but detached", () => {
    state = storeState({ folders: oneLive(), detachedSessions: { sess1: "named-1" } });
    const html = render("sess1");
    expect(html).toContain("This session is detached");
    expect(html).toContain("Resume");
    // Deliberately NOT the chat: the socket stays shut until Resume.
    expect(html).not.toContain('data-chat="sess1"');
    // …and NOT the dead-session wording — this session is still alive.
    expect(html).not.toContain("not running any more");
  });

  it("stays the quiet dead tombstone when the server no longer has it", () => {
    state = storeState({ folders: [{ folder: "", sessions: [session({ live: false })] }] });
    const html = render("sess1");
    expect(html).toContain("This session is not running any more");
    expect(html).toContain("Open its transcript");
    expect(html).not.toContain("detached");
  });
});

/**
 * "A cap that is hit shows the cap and the setting that raises it, never a dead
 * button" (CLAUDE.md, panes §3). The note is the sentence, so it is asserted
 * where it renders rather than where it is spelled: `full && <SessionCap />` is
 * a conditional, and the CSS test that checks the note's colour is green whether
 * or not anything ever mounts it.
 */
describe("the concurrency ceiling, on screen", () => {
  it("says the cap and the env var, and refuses New session, at the ceiling", () => {
    state = storeState({
      folders: oneLive(),
      sessionLimits: { active: 2, max_concurrent: 2 },
    });
    const html = renderBrowser();
    expect(html).toContain("wb-sessions-cap");
    expect(html).toContain("2 of 2 sessions busy");
    // The setting that raises it, named — not just the fact of the limit.
    expect(html).toContain("<code>WORKBENCH_MAX_CONCURRENT_SESSIONS</code>");
    // A note, not an alert: a limit is not a failure (§6.5).
    expect(html).toContain('role="note"');
    expect(newSessionButton(html)).toContain("disabled");
  });

  it("counts sessions *working*, so being over the count is what refuses", () => {
    // The server counts working sessions, not open ones — a listing with one
    // row and a full server still refuses, and a listing with many and a quiet
    // server does not. Reading the numbers off `sessionLimits` is what makes
    // both true; counting rows would grey the button at the wrong moment.
    state = storeState({
      folders: oneLive(),
      sessionLimits: { active: 4, max_concurrent: 4 },
    });
    expect(newSessionButton(renderBrowser())).toContain("disabled");
  });

  it("says nothing and stays live below the ceiling", () => {
    state = storeState({
      folders: oneLive(),
      sessionLimits: { active: 1, max_concurrent: 4 },
    });
    const html = renderBrowser();
    expect(html).not.toContain("wb-sessions-cap");
    expect(html).not.toContain("WORKBENCH_MAX_CONCURRENT_SESSIONS");
    expect(newSessionButton(html)).not.toContain("disabled");
  });

  it("behaves as it always did before the first listing lands", () => {
    // No numbers yet is not "at the cap": the 429 toast is the backstop.
    state = storeState({ folders: oneLive(), sessionLimits: null });
    const html = renderBrowser();
    expect(html).not.toContain("wb-sessions-cap");
    expect(newSessionButton(html)).not.toContain("disabled");
  });

  it("counts the live fleet in the header, and is absent at zero (§6.7)", () => {
    state = storeState({ folders: oneLive() });
    expect(renderBrowser()).toContain("1 live");
    state = storeState({ folders: [{ folder: "", sessions: [session({ live: false })] }] });
    expect(renderBrowser()).not.toContain("wb-sessions-count");
  });
});

/** An empty list should read as chosen, not as a list that failed to arrive. */
describe("the empty session list", () => {
  it("names itself and points at the gesture that fills it", () => {
    state = storeState({ folders: [] });
    const html = renderBrowser();
    expect(html).toContain('<div class="wb-sessions-none-title">No sessions yet</div>');
    expect(html).toContain('<div class="wb-sessions-none-hint">');
    expect(html).toContain("agent session here");
  });

  it("gets out of the way as soon as there is a session", () => {
    state = storeState({ folders: oneLive() });
    expect(renderBrowser()).not.toContain("wb-sessions-none");
  });
});

/**
 * The fleet made legible (§6.12, §7): a row you can read without decoding a
 * hue. Every non-idle tone takes this branch, so every non-idle tone is driven
 * through it — `working` alone would leave the three states that are *cleared
 * on view* rather than by a turn ending untested, which is exactly where a row
 * that never reverts to its timestamp would hide.
 */
describe("a session row says what the session is doing", () => {
  const tones: { name: string; slice: Partial<StoreState>; label: string }[] = [
    { name: "working", slice: { sessionStates: { sess1: "working" } }, label: "Working" },
    {
      name: "needs_attention",
      slice: { sessionStates: { sess1: "needs_attention" } },
      label: "Needs attention",
    },
    {
      name: "errored",
      slice: { sessionFlags: { sess1: { done: false, error: true } } },
      label: "Error",
    },
    {
      name: "done",
      slice: { sessionFlags: { sess1: { done: true, error: false } } },
      label: "Done",
    },
  ];

  for (const tone of tones) {
    it(`reads "${tone.label}" in place of the timestamp when ${tone.name}`, () => {
      state = storeState({ folders: oneLive(), ...tone.slice });
      const row = sessionRow(renderBrowser());
      expect(row).toContain(`<span class="wb-session-state">${tone.label}</span>`);
      // It takes the time cell's place rather than a column of its own, so the
      // row's geometry does not move (§1.9).
      expect(row).not.toContain("wb-session-time");
      // The dot drops out of the accessible name when the word beside it says
      // the same thing — "Working / new session / Working" is one slot claimed
      // twice.
      expect(row).toContain('aria-hidden="true"');
      expect(row).not.toContain('role="img"');
      // The hover title carries it too, for the row that is truncated.
      expect(row).toContain(`title="A conversation — ${tone.label}"`);
    });

    it(`goes quiet again once ${tone.name} clears`, () => {
      // `done` and `error` are cleared *on view* rather than by a turn ending,
      // so "the row reverts" is a separate claim from "the row lit up".
      state = storeState({ folders: oneLive(), ...tone.slice });
      expect(sessionRow(renderBrowser())).toContain("wb-session-state");
      state = storeState({ folders: oneLive() });
      const row = sessionRow(renderBrowser());
      expect(row).not.toContain("wb-session-state");
      expect(row).toContain("wb-session-time");
    });
  }

  it("says nothing at idle — the quiet default, with the dot labelled instead", () => {
    state = storeState({ folders: oneLive() });
    const row = sessionRow(renderBrowser());
    expect(row).not.toContain("wb-session-state");
    expect(row).toContain("wb-session-time");
    expect(row).toContain('role="img"');
    expect(row).toContain('aria-label="Idle"');
    // A *live* row still names its state in the hover title — the row is quiet,
    // not silent. Only a transcript on disk drops the suffix (below).
    expect(row).toContain('title="A conversation — Idle"');
  });

  it("keeps the timestamp for a transcript on disk, whatever the stale state says", () => {
    // A finished session is something you *read*; it is not doing anything, so
    // a leftover `working` in the state map must not make its row claim to be.
    state = storeState({
      folders: [{ folder: "", sessions: [session({ live: false })] }],
      sessionStates: { sess1: "working" },
    });
    const row = sessionRow(renderBrowser());
    expect(row).not.toContain("wb-session-state");
    expect(row).toContain("wb-session-time");
    expect(row).toContain("wb-dot-disk");
    // …and the title says only what it is, with no state to report.
    expect(row).toContain('title="A conversation"');
  });
});
