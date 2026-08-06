/**
 * The conversation browser's own state and its pure parts.
 *
 * A `create()` instance in the capability's own module, per the registration
 * rule in `docs/tools.md`: nothing outside this module and its panel reads it.
 * What *is* shared — sessions, chats, toasts — stays in `ui/src/store.ts` and is
 * reached through the actions it already exposes.
 *
 * The scan is shared; the *search* is not. Two browser panes look at one reading
 * of the store (fetching it twice would read 398 MB twice) but each has its own
 * query and its own scope, which is why the query lives in the component's state
 * and not here. A store field would make two panes one pane wearing two tabs.
 */

import { create } from "zustand";

import * as api from "./api";
import { useStore } from "./store";
import type { ConversationInfo, ConversationStore, ProjectGroup } from "./types";

/** Conversations read per request. Matches the server's own default; sent
 * explicitly so "Read them all" has something to raise. */
export const PAGE = 200;
/** What "Read them all" raises it to — the server's ceiling. */
export const MAX_PAGE = 2000;

interface BrowserState {
  store: ConversationStore | null;
  loading: boolean;
  /** Why the last read failed, in words, or null. */
  error: string | null;
  /** Rows requested last time — carried so a refresh keeps a widened window. */
  limit: number;
  load: (limit?: number) => Promise<void>;
}

export const useConversations = create<BrowserState>((set, get) => ({
  store: null,
  loading: false,
  error: null,
  limit: PAGE,
  load: async (limit) => {
    const want = limit ?? get().limit;
    set({ loading: true, error: null, limit: want });
    try {
      set({ store: await api.getConversations(want), loading: false });
    } catch (err) {
      // Rendered in the panel rather than raised as a toast: this is the
      // panel's own content failing, and a toast would leave it blank.
      set({ loading: false, error: err instanceof Error ? err.message : String(err) });
    }
  },
}));

// ---- pure: search, scoping, formatting --------------------------------------

/** How a group is named on screen. Never a guessed path — an unresolved group
 * shows the key Claude Code stored, which is the only thing we know is true. */
export function groupLabel(group: ProjectGroup): string {
  if (!group.resolved) return group.key;
  if (group.workspace_relative === "") return "workspace root";
  if (group.workspace_relative !== null) return group.workspace_relative;
  return group.label;
}

/**
 * Does this conversation match what was typed?
 *
 * Across the title *and* the folder, because "that conversation about the DST
 * bug" and "everything I did in `src/forecast`" are the same gesture. Match is
 * case-insensitive substring on both — no fuzzy scoring, because a browser you
 * reach for three days later should not surprise you with why a row is there.
 *
 * A row the server has not read yet is matched on its folder only. Its title is
 * a placeholder, and searching a placeholder would put rows under "not" and
 * "read" that have nothing to do with either — the panel's empty state says how
 * many were searched, and "Read them all" is what makes the rest searchable.
 */
export function matches(
  group: ProjectGroup,
  conversation: ConversationInfo,
  query: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (needle === "") return true;
  return (
    (conversation.read && conversation.title.toLowerCase().includes(needle)) ||
    groupLabel(group).toLowerCase().includes(needle) ||
    group.label.toLowerCase().includes(needle)
  );
}

/**
 * The groups a pane shows: filtered by its query, and by its scope if it has one.
 *
 * A group whose every row is filtered out disappears; a group with rows left
 * keeps its header, its label and its refusal reason — the reason is a property
 * of the folder, not of the row, and a search must not hide why something
 * cannot be opened.
 */
export function visibleGroups(
  store: ConversationStore | null,
  query: string,
  scope: string | null,
): ProjectGroup[] {
  if (store === null) return [];
  const groups = scope === null ? store.projects : store.projects.filter((g) => g.key === scope);
  return groups
    .map((group) => ({
      ...group,
      conversations: group.conversations.filter((c) => matches(group, c, query)),
    }))
    .filter((group) => group.conversations.length > 0);
}

/** Turn count as a row says it: a floor when the scan hit its byte budget, and
 * nothing at all when the transcript has not been read — "0 turns" would be a
 * number we never counted. */
export const turnLabel = (conversation: ConversationInfo): string => {
  if (!conversation.read) return "not read";
  return (
    `${String(conversation.turns)}${conversation.turns_capped ? "+" : ""} ` +
    (conversation.turns === 1 && !conversation.turns_capped ? "turn" : "turns")
  );
};

/** Rows on screen right now, for the count the empty/filtered states use. */
export const countRows = (groups: ProjectGroup[]): number =>
  groups.reduce((total, group) => total + group.conversations.length, 0);

// ---- opening a conversation -------------------------------------------------

export type OpenOutcome =
  | { kind: "focus"; sessionId: string }
  | { kind: "resumed"; sessionId: string }
  | { kind: "refused"; reason: string }
  | { kind: "failed" };

/**
 * Transcripts this window has already resumed: SDK id -> live local id.
 *
 * The server answers the same question in `live_session_id`, and that answer is
 * the authority — but it is a *reading*, taken when the store was last scanned.
 * Between clicking a row and the next scan the row still says "not live", and
 * clicking twice would resume the same history into two sessions and two panes.
 * (Found exactly that way: journey 11's second click opened a seventh pane.)
 *
 * A module-level registry keyed by the resource's own id, which is the shipped
 * pattern for this (`terminalInput.ts`, CLAUDE.md "a pane is a view onto a
 * resource it does not own"). It is *validated*, never trusted: an entry whose
 * session is no longer live is dropped and the row resumes afresh, so a session
 * that died cannot leave a row pointing at a dead pane forever.
 */
const resumedHere = new Map<string, string>();

/**
 * Resumes this window has *started* and not yet finished: SDK id -> the promise.
 *
 * `resumedHere` closes the window between one click and the next scan; this
 * closes the one inside a single round trip, which is the window two panes can
 * both be in. Two Conversations panes on overlapping folders is not an exotic
 * setup — it is this feature's headline scenario (one scoped to a project, one
 * unscoped), each `Row` has its own `busy` flag, and a shared map written only
 * *after* `resumeConversation` resolves is empty for the whole flight. Both
 * clicks would see "not live" and both would resume, which is the fork the
 * whole mechanism exists to prevent.
 *
 * Set synchronously, before the first `await`, so there is no interleaving
 * point between the check and the record: a second caller finds the promise and
 * waits on it rather than issuing a resume of its own.
 */
const resuming = new Map<string, Promise<string | null>>();

function alreadyLive(sdkSessionId: string): string | null {
  const local = resumedHere.get(sdkSessionId);
  if (local === undefined) return null;
  const live = useStore
    .getState()
    .folders.some((group) =>
      group.sessions.some((session) => session.session_id === local && session.live),
    );
  if (!live) {
    resumedHere.delete(sdkSessionId);
    return null;
  }
  return local;
}

/** Forget this window's resumes — for tests, and for nothing else. */
export const forgetResumes = (): void => {
  resumedHere.clear();
  resuming.clear();
};

/**
 * What opening a row *means*, with no dock and no React in it.
 *
 * Four answers, and the first two are the reason this is not one line at the
 * call site: a conversation a live session already continues must be *focused*
 * rather than resumed a second time (resuming again forks one history into two
 * sessions), and a folder outside the workspace is refused **with its reason**
 * rather than quietly doing nothing. Half B lifts the third case; until then
 * the row exists, says why, and stays where you can see it.
 *
 * "Already continues" has three tenses, and all three are checked here: the
 * server's `live_session_id` (settled, and the authority), this window's
 * `resumedHere` (settled since the last scan), and `resuming` (in flight right
 * now). Miss the last one and two panes clicking the same row inside one round
 * trip fork it anyway.
 */
export async function openConversation(
  group: ProjectGroup,
  conversation: ConversationInfo,
): Promise<OpenOutcome> {
  const running = conversation.live_session_id ?? alreadyLive(conversation.session_id);
  if (running !== null) return { kind: "focus", sessionId: running };
  const inFlight = resuming.get(conversation.session_id);
  if (inFlight !== undefined) {
    // Someone else is already resuming this transcript. Wait for their session
    // rather than starting a second one — the outcome is `focus`, because by
    // the time this resolves the pane exists and this click did not make it.
    const joined = await inFlight;
    return joined === null ? { kind: "failed" } : { kind: "focus", sessionId: joined };
  }
  if (!group.openable || group.workspace_relative === null) {
    return {
      kind: "refused",
      reason: group.reason ?? "This conversation's folder cannot be opened from here.",
    };
  }
  // Started and recorded with no `await` between them, which is what makes the
  // check above sound: JS runs this to completion before any other click can
  // reach the `resuming.get` above it.
  const pending = useStore
    .getState()
    .resumeConversation(group.workspace_relative, conversation.session_id);
  resuming.set(conversation.session_id, pending);
  let sessionId: string | null;
  try {
    sessionId = await pending;
  } finally {
    // Whatever happened, the flight is over: a failed resume must leave the row
    // clickable rather than permanently joined to a promise that rejected.
    resuming.delete(conversation.session_id);
  }
  if (sessionId === null) return { kind: "failed" };
  resumedHere.set(conversation.session_id, sessionId);
  // Re-read so the row starts carrying the server's own answer (a live dot, and
  // `live_session_id` for a window that reloads). Cheap: the scan is cached
  // against each transcript's mtime, so an unchanged store is enumeration only.
  void useConversations.getState().load();
  return { kind: "resumed", sessionId };
}
