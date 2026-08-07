/**
 * Turning a transcript into chat rows, and deciding when a reattached pane must
 * fetch one.
 *
 * Split out of `store.ts` for one reason: the store reaches `document` and
 * `localStorage` at import, so it cannot be loaded in the node unit environment
 * — while these two decisions are exactly what the reload-replay behaviour turns
 * on and are worth a millisecond-cost test. The integrated path (socket, pane,
 * a real reload) is proved in `e2e/transcript-reload.spec.ts`.
 */

import type { ChatItem, ChatState } from "./store";
import type { TranscriptMessage } from "./types";

/**
 * A stored/live transcript as chat rows.
 *
 * The one mapping, used by both entry points into a chat's history: resuming a
 * conversation into a fresh session, and rehydrating a reattached one after a
 * reload. A transcript carries only settled turns, so every assistant row is
 * `done` with no cost of its own — the live cost lands on the *next* turn's
 * `turn_done`, never retroactively on replayed history.
 */
export function transcriptToChatItems(messages: TranscriptMessage[]): ChatItem[] {
  return messages.map((m) =>
    m.role === "user"
      ? { kind: "user", text: m.text }
      : { kind: "assistant", text: m.text, done: true, costUsd: null, isError: false },
  );
}

/**
 * True when a reattached pane must hydrate this chat from the live transcript.
 *
 * A chat a reload emptied is absent from the store (chats are in-memory), and a
 * session opened but never streamed has an empty `items` — both are the "blank
 * chat over a live socket" this replay exists to fill. A **warm** window that
 * never navigated away still holds the conversation's rows, so it is `false`
 * here: it keeps what it accumulated and skips the fetch, and nothing is
 * counted twice. That guard is the whole of "does not double-count" — a warm
 * chat is never re-fetched, so its history is never prepended in front of
 * itself.
 */
export function chatNeedsHydration(chat: ChatState | undefined): boolean {
  return chat === undefined || chat.items.length === 0;
}
