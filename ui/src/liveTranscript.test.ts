/**
 * The two decisions the reload-replay turns on: what a transcript becomes on
 * screen, and when a reattached pane must fetch one. The socket-and-pane
 * integration around them is proved in `e2e/transcript-reload.spec.ts`.
 */

import { describe, expect, it } from "vitest";

import { chatNeedsHydration, transcriptToChatItems } from "./liveTranscript";
import type { ChatState } from "./store";
import type { TranscriptMessage } from "./types";

const msg = (role: TranscriptMessage["role"], text: string): TranscriptMessage => ({ role, text });

describe("transcriptToChatItems", () => {
  it("maps a user record to a user row and an assistant record to a settled one", () => {
    const items = transcriptToChatItems([
      msg("user", "rewrite the bid curve"),
      msg("assistant", "here is the rewrite"),
    ]);
    expect(items).toEqual([
      { kind: "user", text: "rewrite the bid curve" },
      { kind: "assistant", text: "here is the rewrite", done: true, costUsd: null, isError: false },
    ]);
  });

  it("keeps order and does not fabricate a cost for replayed history", () => {
    const items = transcriptToChatItems([msg("assistant", "a"), msg("assistant", "b")]);
    expect(items.map((i) => (i.kind === "assistant" ? i.text : ""))).toEqual(["a", "b"]);
    // A replayed assistant turn is settled with no cost of its own — the live
    // cost belongs to the next turn, never retroactively to history.
    for (const item of items) {
      if (item.kind === "assistant") expect(item.costUsd).toBeNull();
    }
  });

  it("returns nothing for an empty transcript", () => {
    expect(transcriptToChatItems([])).toEqual([]);
  });
});

describe("chatNeedsHydration", () => {
  const chat = (items: ChatState["items"]): ChatState => ({ items });

  it("hydrates a chat a reload emptied (absent from the store)", () => {
    expect(chatNeedsHydration(undefined)).toBe(true);
  });

  it("hydrates a session opened but never streamed (no rows yet)", () => {
    expect(chatNeedsHydration(chat([]))).toBe(true);
  });

  it("skips a warm chat that already holds rows, so nothing is re-fetched", () => {
    expect(chatNeedsHydration(chat([{ kind: "user", text: "already here" }]))).toBe(false);
  });
});
