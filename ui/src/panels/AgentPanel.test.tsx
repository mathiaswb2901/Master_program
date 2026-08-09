/**
 * The three states a session pane can be in when its binding is judged (M5 item
 * 15). A pane is a *claim* about a session, vetted before it is believed:
 *
 *  - the session is running and was not detached → it streams (the chat mounts);
 *  - the session is running but the user *detached* it → the Resume tombstone,
 *    whose one action reattaches (distinct from the quiet dead case);
 *  - the server no longer has the session → the quiet "not running any more"
 *    tombstone that already shipped, unchanged.
 *
 * Static markup rather than a DOM stack (the suite is node-only — the effects
 * that open the socket never run here, which is the point: this asserts what a
 * pane *shows* before it decides to connect). The store is the real selector run
 * against a hand-built state, exactly as `ActivityPanel.test.tsx` does it.
 */

import type { IDockviewPanelProps } from "dockview";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { FolderSessions, SessionInfo } from "../types";

// Neighbours this pane reaches through but does not exercise here. `../dock` and
// `./Panes` pull in the dock runtime and the whole registry; `./Chat` is the
// body that would mount in the live case — a marker stands in for it so "the
// chat mounted" is observable without Monaco; `./PlanCard` builds a plan draft
// off the real store at module scope (the Agent descriptor spreads its commands).
vi.mock("../dock", () => ({ dockApiHandle: () => null, focusPanel: () => undefined }));
vi.mock("./Panes", () => ({ revealPane: () => undefined }));
vi.mock("./Chat", () => ({
  Chat: ({ sessionId }: { sessionId: string }) => <div data-chat={sessionId}>chat</div>,
  statusVisual: () => ({ color: "", bg: "", pulse: false, label: "", tone: "idle" }),
  dotClass: () => "wb-dot is-idle",
  TranscriptView: () => null,
}));
vi.mock("./PlanCard", () => ({ planCommands: [], planShortcuts: {} }));

interface StoreState {
  folders: FolderSessions[];
  detachedSessions: Record<string, string>;
}

let state: StoreState = { folders: [], detachedSessions: {} };
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

function render(sessionId: string): string {
  const props = { api: { id: `agent#${sessionId}` } } as unknown as IDockviewPanelProps;
  return renderToStaticMarkup(<AgentPanel {...props} />);
}

describe("a session pane, judged against its binding", () => {
  it("streams when the session is live and not detached", () => {
    state = {
      folders: [{ folder: "", sessions: [session({ live: true })] }],
      detachedSessions: {},
    };
    const html = render("sess1");
    expect(html).toContain('data-chat="sess1"');
    expect(html).not.toContain("detached");
    expect(html).not.toContain("not running any more");
  });

  it("offers Resume when the session is live but detached", () => {
    state = {
      folders: [{ folder: "", sessions: [session({ live: true })] }],
      detachedSessions: { sess1: "named-1" },
    };
    const html = render("sess1");
    expect(html).toContain("This session is detached");
    expect(html).toContain("Resume");
    // Deliberately NOT the chat: the socket stays shut until Resume.
    expect(html).not.toContain('data-chat="sess1"');
    // …and NOT the dead-session wording — this session is still alive.
    expect(html).not.toContain("not running any more");
  });

  it("stays the quiet dead tombstone when the server no longer has it", () => {
    state = {
      folders: [{ folder: "", sessions: [session({ live: false })] }],
      detachedSessions: {},
    };
    const html = render("sess1");
    expect(html).toContain("This session is not running any more");
    expect(html).toContain("Open its transcript");
    expect(html).not.toContain("detached");
  });
});
