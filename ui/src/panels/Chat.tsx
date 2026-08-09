import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { Markdown } from "../markdown";
import { useStore, type ChatItem, type SessionFlags } from "../store";
import { toolTargetPath } from "../toolTarget";
import type { SessionState } from "../types";
import { PlanCard } from "./PlanCard";

/**
 * The five states of DESIGN.md §2.6, as names rather than colours.
 *
 * `agent.css` owns what each one looks like (`.wb-dot.is-<tone>`,
 * `.wb-badge.is-<tone>`), so there is exactly one table of the vocabulary and a
 * component never writes a colour. The `color`/`bg` token strings below stay
 * for the two panels that paint their own marks from them (`ActivityPanel`,
 * `MissionControl`) — additive, so neither had to change to land this.
 */
export type AgentTone = "working" | "attention" | "idle" | "done" | "error";

export interface StatusVisual {
  color: string;
  bg: string;
  pulse: boolean;
  label: string;
  tone: AgentTone;
}

/** Maps server state + unseen-result flags to DESIGN.md §2.6 agent-status tokens. */
export function statusVisual(state: SessionState, flags: SessionFlags | undefined): StatusVisual {
  if (state === "working") {
    return {
      color: "var(--agent-working)",
      bg: "var(--agent-working-bg)",
      pulse: true,
      label: "Working",
      tone: "working",
    };
  }
  if (state === "needs_attention") {
    return {
      color: "var(--agent-attention)",
      bg: "var(--agent-attention-bg)",
      pulse: false,
      label: "Needs attention",
      tone: "attention",
    };
  }
  if (flags?.error) {
    return {
      color: "var(--agent-error)",
      bg: "var(--agent-error-bg)",
      pulse: false,
      label: "Error",
      tone: "error",
    };
  }
  if (flags?.done) {
    return {
      color: "var(--agent-done)",
      bg: "var(--agent-done-bg)",
      pulse: false,
      label: "Done",
      tone: "done",
    };
  }
  return {
    color: "var(--agent-idle)",
    bg: "var(--agent-idle-bg)",
    pulse: false,
    label: "Idle",
    tone: "idle",
  };
}

/** The §2.6 dot for a state: the state's name, plus the pulse the one live
 * state gets. The colour lives in `agent.css` — see `statusVisual` above. */
export function dotClass(v: StatusVisual): string {
  return `wb-dot is-${v.tone}` + (v.pulse ? " u-agent-pulse" : "");
}

export function StatusBadge({ state, flags }: { state: SessionState; flags?: SessionFlags }) {
  const v = statusVisual(state, flags);
  return (
    <span className={`wb-badge is-${v.tone}`}>
      <span className={dotClass(v)} aria-hidden="true" />
      {v.label}
    </span>
  );
}

/**
 * The one thing the app is blocked on (DESIGN.md §6.3).
 *
 * Warn-rimmed while it is a question and neutral once it is answered: a card
 * that keeps its alarm after you have decided is a standing warning about
 * something that already happened, which is the §2.4 mistake one hue over.
 *
 * **Everything it renders comes from the store's record for this request id**
 * (`store.permissions`, see `permissions.ts`) — the row in the transcript
 * carries the id and nothing else. The same prompt is also a chip on the Mission
 * Control board and is retracted by the server on a ten-minute timeout, so a
 * card that remembered its own click kept asking a question that was already
 * settled, and answering it again reached a closed prompt (404 by design). One
 * record, every surface: whatever settles it, every card for it settles.
 */
function PermissionCard({ row }: { row: Extract<ChatItem, { kind: "permission" }> }) {
  const item = useStore((s) => s.permissions[row.requestId]);
  const decide = (allow: boolean): void => useStore.getState().decidePermission(row.requestId, allow);
  // Only reachable if a record were dropped while its row survived, which
  // nothing does today: rendering an unanswerable card would be worse.
  if (item === undefined) return null;
  const decided = item.decision !== null;
  return (
    <div className={"wb-perm-card" + (decided ? " is-decided" : "")}>
      <div className="wb-perm-head">
        Permission: <span className="wb-perm-tool">{item.tool}</span>
      </div>
      <div className="wb-perm-body">{item.description}</div>
      {item.decision === null ? (
        <div className="wb-perm-actions">
          <button type="button" className="wb-btn wb-btn-primary" onClick={() => decide(true)}>
            Allow
          </button>
          <button
            type="button"
            className="wb-btn wb-btn-ghost wb-btn-deny"
            onClick={() => decide(false)}
          >
            Deny
          </button>
        </div>
      ) : item.decision === "settled" ? (
        // Closed, and this window never learned which way. Saying "Allowed"
        // here would claim an approval the agent may never have received; the
        // hollow dot is the picker's own "no state to report" shape (§6.12).
        <div className="wb-perm-decision" title="Answered in another window, or timed out">
          <span className="wb-dot is-elsewhere" aria-hidden="true" />
          No longer waiting
        </div>
      ) : (
        <div className="wb-perm-decision">
          {/* The word is the reading; the dot doubles it, because colour is
              never the only signal (§7). */}
          <span
            className={"wb-dot " + (item.decision === "allow" ? "is-allow" : "is-deny")}
            aria-hidden="true"
          />
          {item.decision === "allow" ? "Allowed" : "Denied"}
        </div>
      )}
    </div>
  );
}

/** One tool call: status edge flips on ITS result, chevron expands the output. */
function ToolRow({ item }: { item: Extract<ChatItem, { kind: "tool" }> }) {
  const tree = useStore((s) => s.tree);
  const target = useMemo(() => toolTargetPath(item.summary, tree), [item.summary, tree]);
  const [expanded, setExpanded] = useState(false);
  const hasOutput = item.output !== "";
  // Running / succeeded / failed, said out loud. DESIGN.md §6.3 puts the pulse
  // on the dot and keeps the 2px edge steady, and §7 will not take a colour as
  // the only signal — the edge cannot carry an accessible name, this can.
  const status = item.settled ? (item.settledError ? "Failed" : "Succeeded") : "Running";
  // The server's summary reads on its own ("Read: notes.md") because it also
  // goes to places with no column beside it; here the tool's name is that
  // column, and the row was rendering "Read  Read: notes.md". Display only —
  // `toolTargetPath` above still resolves the file from the whole string.
  const detail = item.summary.startsWith(`${item.tool}: `)
    ? item.summary.slice(item.tool.length + 2)
    : item.summary;
  return (
    <div className="wb-tool">
      <div
        className={
          "wb-tool-row" + (item.settled ? (item.settledError ? " is-failed" : " is-settled") : "")
        }
      >
        <span
          className={"wb-dot" + (item.settled ? "" : " u-agent-pulse")}
          role="img"
          aria-label={status}
          title={status}
        />
        {hasOutput ? (
          <button
            type="button"
            className="wb-tool-chevron"
            aria-expanded={expanded}
            aria-label={expanded ? "Hide output" : "Show output"}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "⌄" : "›"}
          </button>
        ) : (
          <span className="wb-tool-chevron is-empty" aria-hidden="true" />
        )}
        <span className="wb-tool-name">{item.tool}</span>
        {target !== null ? (
          <button
            type="button"
            className="wb-tool-summary wb-tool-link u-truncate"
            title={`Open ${target}`}
            onClick={() => void useStore.getState().openFile(target)}
          >
            {detail}
          </button>
        ) : (
          <span className="wb-tool-summary u-truncate">{detail}</span>
        )}
      </div>
      {expanded && hasOutput && <pre className="wb-tool-output">{item.output}</pre>}
    </div>
  );
}

function ChatItemView({ item }: { item: ChatItem }) {
  switch (item.kind) {
    case "user":
      return <div className="wb-msg-user">{item.text}</div>;
    case "assistant":
      return (
        <div className={item.isError ? "wb-msg-block is-error" : "wb-msg-block"}>
          <Markdown text={item.text} />
          {item.done && item.costUsd !== null && (
            <div className="wb-chat-cost u-tabular">{"$" + item.costUsd.toFixed(4)}</div>
          )}
        </div>
      );
    case "tool":
      return <ToolRow item={item} />;
    case "permission":
      return <PermissionCard row={item} />;
    case "plan":
      return <PlanCard plan={item.plan} />;
    case "error":
      return <div className="wb-msg-error">Agent error: {item.message}</div>;
  }
}

const EMPTY_ITEMS: ChatItem[] = [];

export function Chat({ sessionId }: { sessionId: string }) {
  const items = useStore((s) => s.chats[sessionId]?.items ?? EMPTY_ITEMS);
  const state = useStore((s) => s.sessionStates[sessionId] ?? "idle");
  const flags = useStore((s) => s.sessionFlags[sessionId]);
  const title = useStore((s) => {
    for (const group of s.folders) {
      for (const ses of group.sessions) {
        if (ses.session_id === sessionId) return ses.title;
      }
    }
    return sessionId;
  });

  // The draft lives in the store: prompt shortcuts write it from outside.
  const draft = useStore((s) => s.chatDrafts[sessionId] ?? "");
  const setDraft = (text: string): void => useStore.getState().setChatDraft(sessionId, text);
  const listRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => {
    const el = listRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  const send = (): void => {
    const text = draft.trim();
    if (!text) return;
    useStore.getState().sendChat(text); // clears the draft
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="wb-chat">
      <div className="wb-chat-header">
        <span className="wb-chat-title u-label u-truncate" title={title}>
          {title}
        </span>
        <StatusBadge state={state} flags={flags} />
      </div>
      <div
        ref={listRef}
        className="wb-chat-list"
        onScroll={(e) => {
          const el = e.currentTarget;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        <div className="wb-chat-col">
          {items.length === 0 && (
            <div className="wb-chat-empty">
              <div className="wb-empty-title">Nothing said yet</div>
              <div className="wb-empty-hint">
                Type below and press <span className="wb-keycap">Enter</span>.{" "}
                <span className="wb-keycap">Shift</span> <span className="wb-keycap">Enter</span>{" "}
                starts a new line.
              </div>
            </div>
          )}
          {items.map((item, i) => (
            <ChatItemView key={i} item={item} />
          ))}
        </div>
      </div>
      <div className="wb-chat-input">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Message the agent — Enter to send, Shift+Enter for a new line"
          rows={2}
          spellCheck={false}
        />
        <div className="wb-chat-input-actions">
          {state === "working" && (
            <button
              type="button"
              className="wb-btn wb-btn-outline"
              onClick={() => useStore.getState().interrupt()}
            >
              Interrupt
            </button>
          )}
          <button
            type="button"
            className="wb-btn wb-btn-primary"
            disabled={draft.trim() === ""}
            onClick={send}
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}

export function TranscriptView() {
  const view = useStore((s) => s.transcriptView);
  if (!view) return null;
  return (
    <div className="wb-chat">
      <div className="wb-chat-header">
        <span className="wb-chat-title u-label u-truncate" title={view.session.title}>
          {view.session.title}
        </span>
        <span className="wb-chat-readonly">Transcript</span>
        <button
          type="button"
          className="wb-btn wb-btn-primary"
          onClick={() => void useStore.getState().resumeSession()}
        >
          Resume
        </button>
      </div>
      <div className="wb-chat-list">
        <div className="wb-chat-col">
          {view.messages.length === 0 && <div className="wb-empty-hint">Empty transcript.</div>}
          {view.messages.map((m, i) =>
            m.role === "user" ? (
              <div key={i} className="wb-msg-user">
                {m.text}
              </div>
            ) : (
              <Markdown key={i} text={m.text} />
            ),
          )}
        </div>
      </div>
    </div>
  );
}
