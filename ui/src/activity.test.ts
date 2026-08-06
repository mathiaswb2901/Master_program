/**
 * The reading rules: "now", "just did", fleet order, and the live merge.
 *
 * Every one of these is a place the panel could quietly become wrong without
 * anything failing — a settle read off list position instead of `settled_at`, a
 * merge that patches instead of replacing, an order that reshuffles on equal
 * timestamps. They are pure functions precisely so they can be pinned here.
 */

import { describe, expect, it } from "vitest";

import {
  activeSessions,
  currentEntry,
  folderLabel,
  lastSettledEntry,
  mergeSessions,
  orderSessions,
  outcomeLabel,
  runningCalls,
} from "./activity";
import type { ActivityEntry, SessionActivity } from "./types";

const T = 1_700_000_000;

function entry(id: string, patch: Partial<ActivityEntry> = {}): ActivityEntry {
  return {
    entry_id: id,
    tool: "Read",
    summary: `Read: ${id}.py`,
    target: `${id}.py`,
    started_at: T,
    settled_at: null,
    ok: null,
    ...patch,
  };
}

function session(id: string, entries: ActivityEntry[], activeAt = T): SessionActivity {
  return {
    session_id: id,
    folder: "",
    title: id,
    entries,
    dropped: 0,
    active_at: activeAt,
  };
}

describe("what a session is doing now", () => {
  it("is the newest call still running", () => {
    // Newest first, so the first unsettled entry is the newest one.
    const row = session("s1", [
      entry("c", { started_at: T + 2 }),
      entry("b", { started_at: T + 1, settled_at: T + 3, ok: true }),
      entry("a", { started_at: T }),
    ]);
    expect(currentEntry(row)?.entry_id).toBe("c");
  });

  it("is null when every call has settled — an honest quiet, not a gap", () => {
    const row = session("s1", [entry("a", { settled_at: T + 1, ok: true })]);
    expect(currentEntry(row)).toBeNull();
    expect(currentEntry(session("s1", []))).toBeNull();
  });
});

describe("what a session just did", () => {
  it("is the most recently settled call, not the one nearest the top", () => {
    // Two calls in flight settling out of order: `b` started later but `a`
    // finished later, and "just did" means finished.
    const row = session("s1", [
      entry("b", { started_at: T + 1, settled_at: T + 5, ok: true }),
      entry("a", { started_at: T, settled_at: T + 9, ok: false }),
    ]);
    expect(lastSettledEntry(row)?.entry_id).toBe("a");
  });

  it("is null while nothing has settled yet", () => {
    expect(lastSettledEntry(session("s1", [entry("a")]))).toBeNull();
  });
});

describe("the fleet reading", () => {
  it("counts sessions with a call in flight, and the calls themselves", () => {
    const rows = [
      session("busy", [entry("a"), entry("b")]),
      session("quiet", [entry("c", { settled_at: T + 1, ok: true })]),
      session("idle", []),
    ];
    expect(activeSessions(rows).map((r) => r.session_id)).toEqual(["busy"]);
    expect(runningCalls(rows)).toBe(2);
  });

  it("orders by most recent activity, and stays stable when two tie", () => {
    const rows = [
      session("b", [], T + 5),
      session("a", [], T + 5),
      session("c", [], T + 9),
    ];
    expect(orderSessions(rows).map((r) => r.session_id)).toEqual(["c", "a", "b"]);
    // Same input, same order — an equal timestamp must not reshuffle a fleet
    // that is being watched.
    expect(orderSessions(orderSessions(rows)).map((r) => r.session_id)).toEqual(["c", "a", "b"]);
  });

  it("names the workspace root rather than showing an empty folder", () => {
    expect(folderLabel("")).toBe("workspace root");
    expect(folderLabel("se3/optimizer")).toBe("se3/optimizer");
  });

  it("says how a call ended in words, never in colour alone", () => {
    expect(outcomeLabel(entry("a"))).toBe("Running");
    expect(outcomeLabel(entry("a", { settled_at: T, ok: true }))).toBe("Done");
    expect(outcomeLabel(entry("a", { settled_at: T, ok: false }))).toBe("Failed");
  });
});

describe("merging a live frame", () => {
  it("replaces a changed row whole rather than patching it", () => {
    const before = [session("s1", [entry("a")], T), session("s2", [], T - 1)];
    const changed = session("s1", [entry("b"), entry("a", { settled_at: T + 1, ok: true })], T + 2);
    const after = mergeSessions(before, [changed], []);
    expect(after.map((r) => r.session_id)).toEqual(["s1", "s2"]);
    expect(after[0].entries.map((e) => e.entry_id)).toEqual(["b", "a"]);
  });

  it("drops the sessions the frame says have left", () => {
    const before = [session("s1", [], T), session("s2", [], T - 1)];
    expect(mergeSessions(before, [], ["s1"]).map((r) => r.session_id)).toEqual(["s2"]);
  });

  it("adds a session this window has never seen", () => {
    const after = mergeSessions([], [session("new", [entry("a")], T)], []);
    expect(after.map((r) => r.session_id)).toEqual(["new"]);
  });

  it("lets a removal win over a row in the same frame", () => {
    // Belt and braces: a frame that both changed and removed a session (the LRU
    // dropping the row it just touched) must not leave it on screen.
    const after = mergeSessions([session("s1", [], T)], [session("s1", [entry("a")], T)], ["s1"]);
    expect(after).toEqual([]);
  });
});
