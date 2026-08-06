/**
 * What the panel *says* in each of its states.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design —
 * `vitest.config.ts`), and every case here is a claim about honesty: an empty
 * machine, a store bigger than the page, a folder that will not open, a
 * transcript that would not parse, and a restored pane whose project is gone.
 * The E2E journey covers the parts that need a browser (searching, clicking a
 * row, the pane it lands in).
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ConversationInfo, ConversationStore, ProjectGroup } from "../types";

// The two imports that would otherwise pull in dockview's runtime, Monaco and
// the app store. Nothing here clicks anything.
vi.mock("../dock", () => ({ openPanel: () => undefined }));
vi.mock("./Panes", () => ({ revealPane: () => undefined }));
vi.mock("../store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({ pushToast: () => undefined, resumeConversation: () => Promise.resolve(null) }),
  }),
}));

const { ConversationsBody } = await import("./Conversations");

const conversation = (over: Partial<ConversationInfo> = {}): ConversationInfo => ({
  session_id: "sess-1",
  title: "Fix the DST bug",
  updated_at: Date.now() / 1000 - 120,
  turns: 4,
  turns_capped: false,
  live_session_id: null,
  problem: null,
  ...over,
});

const group = (over: Partial<ProjectGroup> = {}): ProjectGroup => ({
  key: "C--work-repo",
  label: "C:\\work\\repo",
  resolved: true,
  folder: "C:\\work\\repo",
  workspace_relative: "",
  openable: true,
  reason: null,
  updated_at: Date.now() / 1000 - 120,
  conversations: [conversation()],
  ...over,
});

const store = (over: Partial<ConversationStore> = {}): ConversationStore => {
  const projects = over.projects ?? [group()];
  const total = projects.reduce((n, g) => n + g.conversations.length, 0);
  return {
    projects,
    projects_root: "C:\\Users\\x\\.claude\\projects",
    root_exists: true,
    total_projects: projects.length,
    total_conversations: total,
    returned_conversations: total,
    limit: 200,
    scanned_at: Date.now() / 1000,
    ...over,
  };
};

function show(
  value: ConversationStore | null,
  over: { loading?: boolean; error?: string | null; scope?: string | null; query?: string } = {},
): string {
  return renderToStaticMarkup(
    <ConversationsBody
      store={value}
      loading={over.loading ?? false}
      error={over.error ?? null}
      scope={over.scope ?? null}
      query={over.query ?? ""}
      onQuery={() => undefined}
    />,
  );
}

describe("the folder groups", () => {
  it("names each folder and lists its conversations with turns and age", () => {
    const html = show(store());
    expect(html).toContain("workspace root");
    expect(html).toContain("Fix the DST bug");
    expect(html).toContain("4 turns");
    expect(html).toContain("2m");
  });

  it("shows several folders", () => {
    const html = show(
      store({
        projects: [
          group({ key: "a", workspace_relative: "src" }),
          group({ key: "b", workspace_relative: "notes" }),
        ],
      }),
    );
    expect(html).toContain("src");
    expect(html).toContain("notes");
  });
});

describe("what it will not open", () => {
  // The half-A boundary, on screen: the conversation is visible, and the reason
  // it cannot be opened is a sentence next to it rather than an absence.
  it("shows a folder outside the workspace with its reason, and keeps the rows", () => {
    const html = show(
      store({
        projects: [
          group({
            openable: false,
            workspace_relative: null,
            reason: "Outside this workspace. Workbench opens conversations only from…",
          }),
        ],
      }),
    );
    expect(html).toContain("Outside this workspace");
    expect(html).toContain("Fix the DST bug");
    expect(html).toContain("is-blocked");
  });

  it("shows an unresolvable folder under the key that was actually stored", () => {
    const html = show(
      store({
        projects: [
          group({
            key: "C--gone-project",
            resolved: false,
            folder: null,
            workspace_relative: null,
            openable: false,
            reason: "No folder on this machine matches this project — it may have been moved…",
          }),
        ],
      }),
    );
    expect(html).toContain("C--gone-project");
    expect(html).toContain("moved");
  });

  it("marks a transcript it could not read, and still lists it", () => {
    const html = show(
      store({
        projects: [
          group({
            conversations: [
              conversation({ title: "(unreadable transcript)", problem: "damaged", turns: 0 }),
            ],
          }),
        ],
      }),
    );
    expect(html).toContain("unreadable");
    expect(html).toContain("0 turns");
  });
});

describe("the empty states, which are three different facts", () => {
  it("says it is still reading before the first answer", () => {
    expect(show(null)).toContain("Reading Claude");
  });

  it("says a machine with no history has none, and where it looked", () => {
    const html = show(store({ projects: [], total_conversations: 0, root_exists: false }));
    expect(html).toContain("No Claude conversations yet");
    expect(html).toContain(".claude");
    expect(html).toContain("does not exist yet");
  });

  it("tells an empty store apart from a missing one", () => {
    const html = show(store({ projects: [], total_conversations: 0, root_exists: true }));
    expect(html).toContain("which is empty");
  });

  it("says a search matched nothing, and how much it searched", () => {
    const html = show(store(), { query: "nothing like this" });
    expect(html).toContain("No conversation matches");
    expect(html).toContain("nothing like this");
  });

  it("shows a read failure in the panel rather than leaving it blank", () => {
    expect(show(store(), { error: "500 Internal Server Error" })).toContain(
      "500 Internal Server Error",
    );
  });
});

describe("what it did not read", () => {
  // AXI rule 1, applied to a panel: a capped result states what was withheld
  // and offers the control that widens the window.
  it("says how many conversations are still unread, and offers to read them", () => {
    const html = show(store({ total_conversations: 340, returned_conversations: 1 }));
    expect(html).toContain("1 of 340 conversations");
    expect(html).toContain("Read 339 more");
  });

  it("offers nothing to read when everything was read", () => {
    expect(show(store())).not.toContain("more");
  });

  // Nobody watches Claude Code's storage, so a list that looked live would be a
  // lie. The stamp is next to the count rather than only in a tooltip.
  it("says how old the reading is", () => {
    expect(show(store({ scanned_at: Date.now() / 1000 - 300 }))).toContain("read 5m ago");
  });
});

describe("a pane scoped to one project", () => {
  it("shows only that project", () => {
    const html = show(
      store({
        projects: [
          group({ key: "a", workspace_relative: "src" }),
          group({
            key: "b",
            workspace_relative: "notes",
            conversations: [conversation({ session_id: "s2", title: "Weekly notes" })],
          }),
        ],
      }),
      { scope: "b" },
    );
    expect(html).toContain("Weekly notes");
    expect(html).not.toContain("Fix the DST bug");
  });

  // A restored pane is a claim, not evidence (CLAUDE.md, "Panes are instances").
  it("renders a tombstone with the one action that recovers it", () => {
    const html = show(store(), { scope: "a-project-that-is-gone" });
    expect(html).toContain("no conversations any more");
    expect(html).toContain("Show all conversations");
  });
});
