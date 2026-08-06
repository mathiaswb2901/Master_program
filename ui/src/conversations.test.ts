/**
 * The conversation browser's pure half: what a folder is called, what a search
 * matches, and what opening a row *means* before any pane is involved.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ConversationInfo, ConversationStore, ProjectGroup } from "./types";

const resumeConversation = vi.fn<(folder: string, id: string) => Promise<string | null>>();
/** The live listing `alreadyLive` validates against, swapped per test. */
let liveFolders: { sessions: { session_id: string; live: boolean }[] }[] = [];

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, {
    getState: () => ({ resumeConversation, folders: liveFolders }),
  }),
}));

vi.mock("./api", () => ({ getConversations: () => Promise.resolve(null) }));

const {
  countRows,
  forgetResumes,
  groupLabel,
  matches,
  openConversation,
  turnLabel,
  visibleGroups,
} = await import("./conversations");

const conversation = (over: Partial<ConversationInfo> = {}): ConversationInfo => ({
  session_id: "sess-1",
  title: "Fix the DST bug",
  updated_at: 1_000,
  turns: 4,
  turns_capped: false,
  read: true,
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
  updated_at: 1_000,
  conversations: [conversation()],
  ...over,
});

const store = (groups: ProjectGroup[]): ConversationStore => ({
  projects: groups,
  projects_root: "C:\\Users\\x\\.claude\\projects",
  root_exists: true,
  total_projects: groups.length,
  total_conversations: groups.reduce((n, g) => n + g.conversations.length, 0),
  returned_conversations: groups.reduce((n, g) => n + g.conversations.length, 0),
  limit: 200,
  scanned_at: 1_000,
});

// ---- naming a folder ---------------------------------------------------------

describe("what a folder is called", () => {
  it("names the workspace root as such", () => {
    expect(groupLabel(group({ workspace_relative: "" }))).toBe("workspace root");
  });

  it("uses the workspace-relative path inside the workspace", () => {
    expect(groupLabel(group({ workspace_relative: "src/forecast" }))).toBe("src/forecast");
  });

  it("uses the absolute path outside it", () => {
    expect(
      groupLabel(group({ workspace_relative: null, label: "C:\\other\\project" })),
    ).toBe("C:\\other\\project");
  });

  // The encoding is lossy, so an unresolved key is the *only* true thing we
  // have. Showing a reconstructed path would be a guess a user cannot check.
  it("falls back to the stored key, never to a guessed path", () => {
    const unresolved = group({
      key: "C--gone-project",
      resolved: false,
      folder: null,
      workspace_relative: null,
      // The server sets `label` to the key here too; this asserts the client
      // reads the *key* regardless, so a future label can never smuggle a
      // reconstructed path onto the screen.
      label: "C:\\a\\reconstructed\\guess",
    });
    expect(groupLabel(unresolved)).toBe("C--gone-project");
  });
});

// ---- search ------------------------------------------------------------------

describe("search", () => {
  const dst = conversation({ title: "Fix the DST bug in settlement" });
  const bids = conversation({ session_id: "s2", title: "Rewrite the bid curve" });
  const src = group({ workspace_relative: "src/forecast", conversations: [dst, bids] });

  it("matches a title, case-insensitively", () => {
    expect(matches(src, dst, "dst")).toBe(true);
    expect(matches(src, bids, "dst")).toBe(false);
  });

  it("matches the folder too, because 'everything in src' is the same gesture", () => {
    expect(matches(src, bids, "forecast")).toBe(true);
  });

  it("matches everything on an empty or blank query", () => {
    expect(matches(src, bids, "")).toBe(true);
    expect(matches(src, bids, "   ")).toBe(true);
  });

  // A row outside the read budget is listed with a placeholder title. Searching
  // the placeholder would file every unread conversation under "not" and
  // "read"; its folder is real and is still searchable.
  it("does not search the placeholder title of a row it has not read", () => {
    const unread = conversation({ session_id: "s9", title: "(not read yet)", read: false });
    const folder = group({ workspace_relative: "src/forecast", conversations: [unread] });
    expect(matches(folder, unread, "read")).toBe(false);
    expect(matches(folder, unread, "forecast")).toBe(true);
    expect(matches(folder, unread, "")).toBe(true);
  });

  it("drops a folder whose every row was filtered out, and keeps the rest", () => {
    const other = group({
      key: "other",
      workspace_relative: "notes",
      conversations: [conversation({ session_id: "s3", title: "Weekly notes" })],
    });
    const visible = visibleGroups(store([src, other]), "dst", null);
    expect(visible.map((g) => g.key)).toEqual([src.key]);
    expect(countRows(visible)).toBe(1);
  });

  it("keeps a filtered group's refusal reason, which is about the folder", () => {
    const blocked = group({
      openable: false,
      workspace_relative: null,
      reason: "Outside this workspace.",
      conversations: [dst, bids],
    });
    expect(visibleGroups(store([blocked]), "dst", null)[0]?.reason).toBe(
      "Outside this workspace.",
    );
  });
});

describe("a pane scoped to one project", () => {
  const a = group({ key: "a", conversations: [conversation({ session_id: "a1" })] });
  const b = group({ key: "b", conversations: [conversation({ session_id: "b1" })] });

  it("shows only its own folder", () => {
    expect(visibleGroups(store([a, b]), "", "b").map((g) => g.key)).toEqual(["b"]);
  });

  it("shows everything when it has no scope", () => {
    expect(visibleGroups(store([a, b]), "", null)).toHaveLength(2);
  });

  // Layouts persist, resources do not: a pane restored onto a project that has
  // no conversations any more must render as itself, not as an empty list.
  it("shows nothing at all when its project is gone", () => {
    expect(visibleGroups(store([a]), "", "b")).toEqual([]);
  });
});

// ---- turn counts -------------------------------------------------------------

describe("turn counts", () => {
  it("says the number plainly", () => {
    expect(turnLabel(conversation({ turns: 4 }))).toBe("4 turns");
    expect(turnLabel(conversation({ turns: 1 }))).toBe("1 turn");
  });

  // A count the scan could not finish is a floor, and says so rather than
  // reporting a smaller number as if it were the answer.
  it("marks a count the byte budget cut short", () => {
    expect(turnLabel(conversation({ turns: 120, turns_capped: true }))).toBe("120+ turns");
  });

  // `turns` is 0 on a row nobody read, and "0 turns" is a number we never
  // counted — the one thing this panel must not do is state a fact it made up.
  it("says a row was not read rather than reporting zero turns", () => {
    expect(turnLabel(conversation({ turns: 0, read: false }))).toBe("not read");
  });
});

// ---- opening -----------------------------------------------------------------

describe("opening a conversation", () => {
  beforeEach(() => {
    forgetResumes();
    liveFolders = [];
    resumeConversation.mockReset();
  });

  it("focuses the live session that already continues it, rather than forking", async () => {
    const outcome = await openConversation(
      group(),
      conversation({ live_session_id: "local-7" }),
    );
    expect(outcome).toEqual({ kind: "focus", sessionId: "local-7" });
    expect(resumeConversation).not.toHaveBeenCalled();
  });

  // The bug journey 11 caught: the row still says "not live" until the next
  // scan, so a second click resumed the same history into a second pane.
  it("does not resume the same conversation twice before the next scan lands", async () => {
    resumeConversation.mockResolvedValue("local-new");
    const c = conversation({ session_id: "sdk-9" });
    expect(await openConversation(group(), c)).toEqual({
      kind: "resumed",
      sessionId: "local-new",
    });

    liveFolders = [{ sessions: [{ session_id: "local-new", live: true }] }];
    expect(await openConversation(group(), c)).toEqual({
      kind: "focus",
      sessionId: "local-new",
    });
    expect(resumeConversation).toHaveBeenCalledTimes(1);
  });

  // …and the memory is never trusted on its own: a session that died leaves a
  // row that resumes afresh rather than one that focuses a dead pane.
  it("resumes again once the session it remembers is gone", async () => {
    resumeConversation.mockResolvedValue("local-a");
    const c = conversation({ session_id: "sdk-9" });
    await openConversation(group(), c);

    liveFolders = [{ sessions: [{ session_id: "local-a", live: false }] }];
    resumeConversation.mockResolvedValue("local-b");
    expect(await openConversation(group(), c)).toEqual({
      kind: "resumed",
      sessionId: "local-b",
    });
    expect(resumeConversation).toHaveBeenCalledTimes(2);
  });

  // `resumedHere` is written only *after* the resume resolves, so for the whole
  // round trip it is empty and the row still says "not live". Two Conversations
  // panes on overlapping folders — one scoped to a project, one unscoped, which
  // is this feature's headline scenario — each have their own `busy` flag, so
  // one click in each inside that flight both passed the guard and both
  // resumed: one history, two sessions, two panes.
  it("joins a resume already in flight instead of forking a second one", async () => {
    let settle: (id: string | null) => void = () => undefined;
    resumeConversation.mockReturnValue(
      new Promise<string | null>((resolve) => {
        settle = resolve;
      }),
    );
    const c = conversation({ session_id: "sdk-9" });

    // Both panes click before either round trip has come back.
    const first = openConversation(group(), c);
    const second = openConversation(group(), c);
    settle("local-new");

    expect(await first).toEqual({ kind: "resumed", sessionId: "local-new" });
    // Focus, not resumed: by the time this settles the pane exists, and this
    // click is not what made it.
    expect(await second).toEqual({ kind: "focus", sessionId: "local-new" });
    expect(resumeConversation).toHaveBeenCalledTimes(1);
  });

  // The flight has to end whatever happened to it, or a resume that failed
  // would leave the row joined forever to a promise that never produced a pane.
  it("lets a row be clicked again after a resume in flight failed", async () => {
    resumeConversation.mockResolvedValueOnce(null);
    const c = conversation({ session_id: "sdk-9" });
    expect((await openConversation(group(), c)).kind).toBe("failed");

    resumeConversation.mockResolvedValueOnce("local-2");
    expect(await openConversation(group(), c)).toEqual({
      kind: "resumed",
      sessionId: "local-2",
    });
    expect(resumeConversation).toHaveBeenCalledTimes(2);
  });

  it("resumes one that is only on disk, in its own folder", async () => {
    resumeConversation.mockClear();
    resumeConversation.mockResolvedValue("local-new");
    const outcome = await openConversation(
      group({ workspace_relative: "src" }),
      conversation({ session_id: "sdk-9" }),
    );
    expect(resumeConversation).toHaveBeenCalledWith("src", "sdk-9");
    expect(outcome).toEqual({ kind: "resumed", sessionId: "local-new" });
  });

  // Half A's boundary, and the shape it takes: a refusal that carries the
  // reason, not a row that quietly does nothing.
  it("refuses a folder outside the workspace and hands back why", async () => {
    resumeConversation.mockClear();
    const outcome = await openConversation(
      group({
        openable: false,
        workspace_relative: null,
        reason: "Outside this workspace. Workbench opens conversations only from…",
      }),
      conversation(),
    );
    expect(outcome).toEqual({
      kind: "refused",
      reason: "Outside this workspace. Workbench opens conversations only from…",
    });
    expect(resumeConversation).not.toHaveBeenCalled();
  });

  it("refuses an unresolved folder even without a server-supplied reason", async () => {
    const outcome = await openConversation(
      group({ openable: false, workspace_relative: null, reason: null }),
      conversation(),
    );
    expect(outcome.kind).toBe("refused");
  });

  it("reports a failed resume rather than pretending a pane was opened", async () => {
    resumeConversation.mockResolvedValue(null);
    expect((await openConversation(group(), conversation())).kind).toBe("failed");
  });
});
