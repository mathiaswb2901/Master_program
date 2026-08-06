/**
 * Every Claude conversation, grouped by folder — the Claude Code resume list,
 * natively.
 *
 * The owner asked for this in as many words: *"For Claude I also need to have an
 * overview like we do in Claude Code with which chats belong to which folder."*
 * The data was already there (`services/session_index.py` reads Claude Code's
 * own storage); what was missing was a surface that shows all of it rather than
 * the slice bound to the folders this workspace happens to contain.
 *
 * ## What a pane of this is
 *
 * Plural, and the binding is a **project key**:
 *
 *  - `conversations` — everything on this machine;
 *  - `conversations#<encoded-project-key>` — one folder's history, which is what
 *    lets one pane watch a project while another watches the lot.
 *
 * The key is Claude Code's own encoded directory name, so it survives a restart
 * without us storing a path (and a path is exactly what we do not have — the
 * encoding is not reversible). A restored pane whose project is gone from the
 * store renders a tombstone with the one action that recovers it: show all
 * conversations. `docs/tools.md`, "the instance key is a contract".
 *
 * ## Honesty, which is most of the design here
 *
 * This reads storage we do not own and cannot watch, so the panel says four
 * things a prettier one would leave out:
 *
 * 1. **How old the reading is**, with a refresh button — there is no watcher on
 *    `~/.claude/projects`, so a live-looking list would be a lie.
 * 2. **Why a folder cannot be opened**, on the group, in words. A conversation
 *    outside this workspace is *shown and refused*, never hidden: knowing it
 *    exists is most of what a browser is for, and the jail that stops us opening
 *    it is half B's job (the workspace switcher / multi-root roots).
 * 3. **What it could not read** — a damaged transcript keeps its row and carries
 *    the reason, because a conversation that exists must not vanish because our
 *    parser met a half-written line.
 * 4. **What it did not read**: a store bigger than the page says so and offers
 *    the wider window, rather than showing a prefix as if it were everything.
 */

import type { IDockviewPanelProps } from "dockview";
import { useEffect, useState } from "react";

import {
  countRows,
  groupLabel,
  MAX_PAGE,
  openConversation,
  turnLabel,
  useConversations,
  visibleGroups,
} from "../conversations";
import { openPanel } from "../dock";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import { relativeTime, relativeTimePhrase } from "../relativeTime";
import { useStore } from "../store";
import type { ConversationInfo, ConversationStore, ProjectGroup } from "../types";
import { revealPane } from "./Panes";

import "../styles/conversations.css";

const TOOL_ID = "conversations";
/** The tool a row opens into. Its panes are `agent#<session id>`. */
const AGENT_TOOL_ID = "agent";

function HistoryIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M2.5 8a5.5 5.5 0 1 0 1.7-4" />
      <path d="M2 2.5V6h3.5" />
      <path d="M8 5v3l2 1.5" />
    </svg>
  );
}

// ---- one row ----------------------------------------------------------------

function Row({ group, conversation }: { group: ProjectGroup; conversation: ConversationInfo }) {
  const [busy, setBusy] = useState(false);
  const live = conversation.live_session_id !== null;
  const blocked = !group.openable && !live;

  const open = (): void => {
    if (busy) return;
    setBusy(true);
    void openConversation(group, conversation)
      .then((outcome) => {
        if (outcome.kind === "refused") {
          useStore.getState().pushToast("warn", outcome.reason);
          return;
        }
        if (outcome.kind === "failed") return; // the store raised the toast
        // Focus or open — `revealPane` decides, and the decision is pane
        // identity: `agent#<id>` is already on screen or it is not.
        revealPane(AGENT_TOOL_ID, outcome.sessionId);
      })
      .finally(() => setBusy(false));
  };

  return (
    <button
      type="button"
      className={"wb-conv-row" + (blocked ? " is-blocked" : "") + (busy ? " is-busy" : "")}
      onClick={open}
      // The reason travels with the control, so a pointer user gets it without
      // clicking and a screen reader gets it from the accessible description.
      title={
        blocked
          ? (group.reason ?? "")
          : `${conversation.title} — ${relativeTimePhrase(conversation.updated_at)}`
      }
    >
      <span
        className={"wb-dot" + (live ? "" : " wb-dot-disk")}
        style={live ? { background: "var(--agent-idle)" } : undefined}
        role="img"
        aria-label={live ? "Already open" : "On disk"}
      />
      <span className="wb-conv-title u-truncate">{conversation.title}</span>
      <span className="wb-conv-turns u-tabular">{turnLabel(conversation)}</span>
      <span className="wb-conv-time u-tabular">{relativeTime(conversation.updated_at)}</span>
      {conversation.problem !== null && (
        <span className="wb-conv-problem" title={conversation.problem}>
          unreadable
        </span>
      )}
    </button>
  );
}

// ---- one folder -------------------------------------------------------------

function Group({ group }: { group: ProjectGroup }) {
  return (
    <section className="wb-conv-group">
      <header className="wb-conv-group-head">
        <span className="wb-conv-group-name u-label u-truncate" title={group.label}>
          {groupLabel(group)}
        </span>
        <span className="wb-conv-group-count u-tabular">{group.conversations.length}</span>
      </header>
      {/* The refusal is on the folder because that is what it is about, and it
          is a sentence rather than a lock glyph: DESIGN.md §7 — colour and
          iconography are never the only signal. */}
      {group.reason !== null && (
        <p className="wb-conv-reason" role="note">
          {group.reason}
        </p>
      )}
      {group.conversations.map((conversation) => (
        <Row key={conversation.session_id} group={group} conversation={conversation} />
      ))}
    </section>
  );
}

// ---- the panel --------------------------------------------------------------

export function ConversationsPanel(props: IDockviewPanelProps) {
  const scope = paneInstance(props.api.id);
  const store = useConversations((s) => s.store);
  const loading = useConversations((s) => s.loading);
  const error = useConversations((s) => s.error);
  // Per pane, deliberately: two browsers are two searches (see `conversations.ts`).
  const [query, setQuery] = useState("");

  useEffect(() => {
    // One reading, shared: a second pane mounting while the first is loading
    // must not start a second 400 MB scan.
    if (useConversations.getState().store === null && !useConversations.getState().loading) {
      void useConversations.getState().load();
    }
  }, []);

  return (
    <ConversationsBody
      store={store}
      loading={loading}
      error={error}
      scope={scope}
      query={query}
      onQuery={setQuery}
    />
  );
}

export interface BodyProps {
  store: ConversationStore | null;
  loading: boolean;
  error: string | null;
  /** Project key this pane is scoped to, or null for everything. */
  scope: string | null;
  query: string;
  onQuery: (value: string) => void;
}

/** The whole rendering, as a function of its inputs — so the states a browser
 * journey cannot reach in time (an empty machine, a failed read, a scoped pane
 * whose project is gone) are asserted in milliseconds instead. */
export function ConversationsBody({
  store,
  loading,
  error,
  scope,
  query,
  onQuery,
}: BodyProps) {
  const groups = visibleGroups(store, query, scope);
  const shown = countRows(groups);
  const scopeMissing =
    scope !== null && store !== null && !store.projects.some((group) => group.key === scope);

  return (
    <div className="wb-conv">
      <header className="wb-conv-head">
        <input
          className="wb-conv-search"
          type="search"
          value={query}
          placeholder="Search conversations and folders"
          aria-label="Search conversations"
          onChange={(event) => onQuery(event.target.value)}
        />
        <button
          type="button"
          className="wb-btn wb-btn-sm wb-btn-outline"
          disabled={loading}
          onClick={() => void useConversations.getState().load()}
          title={
            store === null
              ? "Read Claude's conversation store"
              : `Read ${relativeTimePhrase(store.scanned_at)} — Claude Code's storage is not watched`
          }
        >
          {loading ? "Reading…" : "Refresh"}
        </button>
      </header>

      {error !== null && (
        <p className="wb-conv-error" role="status">
          Could not read Claude&apos;s conversations: {error}
        </p>
      )}

      {scopeMissing && (
        <div className="wb-pane-note">
          <span className="wb-pane-note-msg u-truncate">
            This folder has no conversations any more.
          </span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => openPanel(TOOL_ID)}
          >
            Show all conversations
          </button>
        </div>
      )}

      <div className="wb-conv-list">
        {groups.map((group) => (
          <Group key={group.key} group={group} />
        ))}
        {shown === 0 && !loading && !scopeMissing && (
          <Empty store={store} query={query} scope={scope} />
        )}
      </div>

      {store !== null && shown > 0 && <Footer store={store} shown={shown} query={query} />}
    </div>
  );
}

/**
 * The three ways there is nothing to show, told apart.
 *
 * "No matches", "you have no Claude history yet" and "still reading" are three
 * different facts, and a single "Nothing here" would make the first two
 * indistinguishable from a bug (DESIGN.md §6.10; an empty result says that it
 * is empty).
 */
function Empty({
  store,
  query,
  scope,
}: {
  store: ConversationStore | null;
  query: string;
  scope: string | null;
}) {
  if (store === null) {
    return <p className="wb-conv-note">Reading Claude&apos;s conversations…</p>;
  }
  if (query.trim() !== "") {
    return (
      <div className="wb-empty">
        <div className="wb-empty-title">No conversation matches “{query}”</div>
        <div className="wb-empty-hint">
          {scope === null
            ? `Searched ${String(store.returned_conversations)} conversations across ` +
              `${String(store.projects.length)} folders — titles and folder names.`
            : "This pane is scoped to one folder; the unscoped browser searches them all."}
        </div>
      </div>
    );
  }
  return (
    <div className="wb-empty">
      <span className="wb-empty-icon">
        <HistoryIcon />
      </span>
      <div className="wb-empty-title">No Claude conversations yet</div>
      <div className="wb-empty-hint">
        {store.root_exists
          ? `Claude Code stores them in ${store.projects_root}, which is empty. ` +
            "Start a session and it will appear here."
          : `Claude Code stores them in ${store.projects_root}, which does not exist yet.`}
      </div>
    </div>
  );
}

/**
 * Re-render on a slow tick, so "read 4m ago" becomes "5m ago" without an event.
 *
 * A minute, because these stamps are coarse and a ticking clock is exactly the
 * motion DESIGN.md §5 rules out. Same pattern, and the same reason, as the usage
 * meters: there is nothing to subscribe to, so the alternative is a number that
 * silently stops being true.
 */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);
  return now;
}

/** What was read, when, and what was not — with the control that widens it. */
function Footer({
  store,
  shown,
  query,
}: {
  store: ConversationStore;
  shown: number;
  query: string;
}) {
  const withheld = store.total_conversations - store.returned_conversations;
  const now = useNow();
  return (
    <footer className="wb-conv-foot">
      <span className="wb-conv-note">
        {query.trim() === ""
          ? `${String(shown)} of ${String(store.total_conversations)} conversations`
          : `${String(shown)} matching of ${String(store.returned_conversations)} read`}
        {/* There is no watcher on Claude Code's storage, so the panel says how
            old this reading is instead of implying it is live. */}
        {` · read ${relativeTimePhrase(store.scanned_at, now / 1000)}`}
      </span>
      {withheld > 0 && (
        <button
          type="button"
          className="wb-btn wb-btn-sm wb-btn-outline"
          onClick={() => void useConversations.getState().load(MAX_PAGE)}
          title={`${String(withheld)} older conversations have not been read yet`}
        >
          Read {withheld} more
        </button>
      )}
    </footer>
  );
}

// ---- registration -----------------------------------------------------------

/** Groups this browser can be scoped to, newest folder first. Empty until the
 * first read lands, which is honest: the picker offers what is known. */
const scopeOptions = (): ProjectGroup[] => useConversations.getState().store?.projects ?? [];

export const conversationsTool: WorkbenchTool = {
  id: TOOL_ID,
  title: "Conversations",
  icon: HistoryIcon,
  panel: {
    component: ConversationsPanel,
    defaultLocation: { area: "right", size: 420 },
    // Not in the startup layout: this is somewhere you go to find something,
    // not a pane that should cost you space while you work.
    openByDefault: false,
    // Plural, bound to a project key — one pane on a project, another on
    // everything. Closing a pane closes a view: the reading is shared and
    // outlives any one of them, and nothing server-side is owned by a pane.
    singleton: false,
    instances: {
      options: () =>
        scopeOptions().map((group) => ({
          id: `conversations.${group.key}`,
          title: `Conversations in ${groupLabel(group)}`,
          detail: `${String(group.conversations.length)} · ${relativeTime(group.updated_at)}`,
          category: "Conversations",
          key: () => group.key,
        })),
      titleFor: (key) => {
        const group = scopeOptions().find((candidate) => candidate.key === key);
        // A restored pane whose project has not loaded yet — or is gone — still
        // has to read as something. The key is what we truly know.
        return group === undefined ? key : groupLabel(group);
      },
    },
  },
  commands: [
    {
      id: "conversations.open",
      title: "Browse Claude conversations",
      detail: () => "every conversation on this machine, grouped by folder",
      run: () => {
        openPanel(TOOL_ID);
        void useConversations.getState().load();
      },
    },
  ],
  // No chord. A registered chord beats the user's `shortcuts.md` one and Alt is
  // all that file may bind; this is a place you go deliberately, and the
  // QuickBar is that gesture (same reasoning as the Usage panel).
  // No statusContribution either: nothing here changes while you are not
  // looking, so a permanent bar item would be chrome with no news in it.
};
