/**
 * What settles a permission prompt, and what must not.
 *
 * The rules the two-surface bug turned on, at the level a millisecond can check
 * them: a replayed prompt must not un-answer itself, a retraction frame must not
 * invent a verdict, and one session's frame must say nothing about another's.
 * The integrated claim — a card in a chat pane settling from a click on the
 * Mission Control board — is `e2e/agent-surfaces.spec.ts`, because it is
 * about two channels meeting in one window and nothing smaller can prove that.
 */

import { describe, expect, it } from "vitest";

import {
  notePermission,
  settlePermission,
  settleVanished,
  type PermissionRecord,
} from "./permissions";

const ASK = { requestId: "req-1", sessionId: "sess-a", tool: "Bash", description: "echo hi" };

/** One open prompt, the state every case below starts from. */
function asking(): Record<string, PermissionRecord> {
  return notePermission({}, ASK);
}

describe("noting a prompt", () => {
  it("records it as a question, with the session it belongs to", () => {
    const records = asking();
    expect(records[ASK.requestId]).toEqual({ ...ASK, decision: null });
  });

  it("does not un-answer one on replay", () => {
    // `/ws/agent` replays every still-pending prompt on each reconnect, and a
    // window that answered one a moment before the socket blipped would get its
    // own question back — with the buttons live under a decision already sent.
    const answered = settlePermission(asking(), ASK.requestId, "allow");
    expect(notePermission(answered, ASK)).toBe(answered);
  });
});

describe("settling one", () => {
  it("keeps the first answer, never the frame that follows it", () => {
    // The retraction frame carries no verdict; overwriting "allow" with the
    // "we were not told" outcome would downgrade a decision the agent has.
    const allowed = settlePermission(asking(), ASK.requestId, "allow");
    expect(settlePermission(allowed, ASK.requestId, "settled")).toBe(allowed);
    expect(allowed[ASK.requestId]?.decision).toBe("allow");
  });

  it("says nothing about a prompt it has no record of", () => {
    const records = asking();
    expect(settlePermission(records, "req-unknown", "deny")).toBe(records);
  });
});

describe("the fleet frame's whole open set", () => {
  it("settles what has left it, without claiming which way it went", () => {
    const settled = settleVanished(asking(), "sess-a", []);
    expect(settled[ASK.requestId]?.decision).toBe("settled");
  });

  it("leaves a prompt that is still open alone", () => {
    const records = asking();
    expect(settleVanished(records, "sess-a", [ASK.requestId])).toBe(records);
  });

  it("touches only the session the frame is about", () => {
    // Two sessions can be blocked at once, and a frame is one session's report.
    const two = notePermission(asking(), {
      requestId: "req-2",
      sessionId: "sess-b",
      tool: "Write",
      description: "notes.md",
    });
    const settled = settleVanished(two, "sess-a", []);
    expect(settled["req-1"]?.decision).toBe("settled");
    expect(settled["req-2"]?.decision).toBeNull();
  });

  it("settles every open prompt of a session that is closing", () => {
    // The abandon path publishes an empty set for the session, and both of its
    // cards have to stop asking.
    const two = notePermission(asking(), {
      requestId: "req-2",
      sessionId: "sess-a",
      tool: "Write",
      description: "notes.md",
    });
    const settled = settleVanished(two, "sess-a", []);
    expect(Object.values(settled).map((record) => record.decision)).toEqual([
      "settled",
      "settled",
    ]);
  });
});
