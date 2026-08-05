/**
 * Scratchpad — the registry's worked example.
 *
 * A whole capability in one file: a panel that opens on demand and closes
 * again, the command that opens it, and its tab icon. Adding it to Workbench
 * cost this module plus one line in `tools.ts`; it touches no file another work
 * lane is likely to touch — no `App.tsx`, no `commands.ts`, no `StatusBar.tsx`,
 * no CSS bundle. That is the tool registry's exit criterion, demonstrated
 * rather than claimed (docs/tools.md walks through it).
 *
 * Behaviour is deliberately small: a textarea persisted to the workspace's own
 * `.workbench/scratch.md` through the existing files API, so notes survive a
 * reload and are readable (and editable) as a plain file like everything else.
 * Styling is inline because a one-file tool has no stylesheet — every value is
 * a DESIGN.md token, never a literal colour.
 */

import type { IDockviewPanelProps } from "dockview";
import { useCallback, useEffect, useRef, useState } from "react";

import * as api from "../api";
import { openPanel } from "../dock";
import type { WorkbenchTool } from "../registry";

const SCRATCH_PATH = ".workbench/scratch.md";
const SAVE_DEBOUNCE_MS = 600;

const PLACEHOLDER = "Notes, snippets, anything. Saved to .workbench/scratch.md.";

function PadIcon() {
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
      <path d="M3.5 1.5h9a.5.5 0 0 1 .5.5v12a.5.5 0 0 1-.5.5h-9a.5.5 0 0 1-.5-.5V2a.5.5 0 0 1 .5-.5ZM5.5 5h5M5.5 8h5M5.5 11h3" />
    </svg>
  );
}

export function ScratchpadPanel(_props: IDockviewPanelProps) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState("Loading…");
  const timerRef = useRef<number | undefined>(undefined);
  const pendingRef = useRef<string | null>(null);

  const flush = useCallback(async (): Promise<void> => {
    const content = pendingRef.current;
    if (content === null) return;
    pendingRef.current = null;
    try {
      // No expected_hash: a scratchpad is single-writer by nature, and a
      // conflict bar over a notes box would be worse than the last word.
      await api.putFileContent({ path: SCRATCH_PATH, content });
      setStatus("Saved");
    } catch (err) {
      setStatus(err instanceof api.ApiError ? `Not saved: ${err.detail}` : "Not saved");
    }
  }, []);

  useEffect(() => {
    let live = true;
    void api
      .getFileContent(SCRATCH_PATH)
      .then((file) => {
        if (!live) return;
        setText(file.content);
        setStatus("Saved");
      })
      .catch(() => {
        // Nothing written yet — the first save creates the file (and its folder).
        if (live) setStatus("Empty");
      });
    return () => {
      live = false;
      window.clearTimeout(timerRef.current);
      void flush();
    };
  }, [flush]);

  const onChange = (value: string): void => {
    setText(value);
    setStatus("Saving…");
    pendingRef.current = value;
    window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => void flush(), SAVE_DEBOUNCE_MS);
  };

  return (
    <div
      className="wb-scratchpad"
      style={{ height: "100%", display: "flex", flexDirection: "column" }}
    >
      <textarea
        className="wb-scratchpad-input"
        value={text}
        placeholder={PLACEHOLDER}
        spellCheck={false}
        aria-label="Scratchpad"
        onChange={(e) => onChange(e.target.value)}
        onBlur={() => {
          window.clearTimeout(timerRef.current);
          void flush();
        }}
        style={{
          flex: 1,
          resize: "none",
          border: "none",
          outline: "none",
          padding: "var(--space-4) var(--space-5)",
          background: "var(--surface-panel)",
          color: "var(--text-primary)",
          fontFamily: "var(--font-mono)",
          fontSize: "var(--type-code-size)",
          lineHeight: "var(--type-code-line)",
        }}
      />
      <div
        className="wb-scratchpad-status u-label"
        role="status"
        style={{
          padding: "var(--space-2) var(--space-5)",
          display: "flex",
          alignItems: "center",
          borderTop: "1px solid var(--border-subtle)",
        }}
      >
        {status}
      </div>
    </div>
  );
}

export const scratchpadTool: WorkbenchTool = {
  id: "scratchpad",
  title: "Scratchpad",
  icon: PadIcon,
  panel: {
    component: ScratchpadPanel,
    defaultLocation: { area: "right", size: 380 },
    // Not in the startup layout: the default arrangement is unchanged for
    // anyone who never opens it — and its tab gets a close button, so the split
    // it makes is undoable by the tab it arrived on.
    openByDefault: false,
  },
  commands: [
    {
      id: "scratchpad.open",
      title: "Open Scratchpad",
      detail: () => SCRATCH_PATH,
      run: () => openPanel("scratchpad"),
    },
  ],
  // No chord, deliberately. A registered command wins every collision with a
  // user's `shortcuts.md`, and Alt is the only modifier that file may use — so
  // a chord claimed here is taken from the user, silently. That price is worth
  // paying for Alt+T; it is not worth paying for the registry's worked example.
  // `shortcuts` is demonstrated by the tools that earn one (docs/tools.md).
};
