import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";

import { Markdown } from "../markdown";
import { useStore, type ChatItem, type SessionFlags } from "../store";
import { toolTargetPath } from "../toolTarget";
import type { SessionState } from "../types";
import { PlanCard } from "./PlanCard";

export interface StatusVisual {
  color: string;
  bg: string;
  pulse: boolean;
  label: string;
}

/** Maps server state + unseen-result flags to DESIGN.md §2.6 agent-status tokens. */
export function statusVisual(state: SessionState, flags: SessionFlags | undefined): StatusVisual {
  if (state === "working") {
    return { color: "var(--agent-working)", bg: "var(--agent-working-bg)", pulse: true, label: "Working" };
  }
  if (state === "needs_attention") {
    return { color: "var(--agent-attention)", bg: "var(--agent-attention-bg)", pulse: false, label: "Needs attention" };
  }
  if (flags?.error) {
    return { color: "var(--agent-error)", bg: "var(--agent-error-bg)", pulse: false, label: "Error" };
  }
  if (flags?.done) {
    return { color: "var(--agent-done)", bg: "var(--agent-done-bg)", pulse: false, label: "Done" };
  }
  return { color: "var(--agent-idle)", bg: "var(--agent-idle-bg)", pulse: false, label: "Idle" };
}

export function StatusBadge({ state, flags }: { state: SessionState; flags?: SessionFlags }) {
  const v = statusVisual(state, flags);
  return (
    <span className="wb-badge" style={{ background: v.bg, color: v.color }}>
      <span
        className={"wb-dot" + (v.pulse ? " u-agent-pulse" : "")}
        style={{ background: v.color }}
        aria-hidden="true"
      />
      {v.label}
    </span>
  );
}

function PermissionCard({ item }: { item: Extract<ChatItem, { kind: "permission" }> }) {
  const decide = (allow: boolean): void => useStore.getState().decidePermission(item.requestId, allow);
  return (
    <div className="wb-perm-card">
      <div className="wb-perm-head">Permission: {item.tool}</div>
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
      ) : (
        <div className="wb-perm-decision">{item.decision === "allow" ? "Allowed" : "Denied"}</div>
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
  return (
    <div className="wb-tool">
      <div
        className={
          "wb-tool-row" + (item.settled ? (item.settledError ? " is-failed" : " is-settled") : "")
        }
      >
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
            {item.summary}
          </button>
        ) : (
          <span className="wb-tool-summary u-truncate">{item.summary}</span>
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
      return <PermissionCard item={item} />;
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

  const [draft, setDraft] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  useEffect(() => {
    const el = listRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  const send = (): void => {
    const text = draft.trim();
    if (!text) return;
    useStore.getState().sendChat(text);
    setDraft("");
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
            <div className="wb-empty-hint">Send a message to start the conversation.</div>
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
