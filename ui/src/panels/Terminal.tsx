import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import type { DockviewPanelApi, IDockviewPanelProps } from "dockview";
import { useEffect, useRef, useState } from "react";

import { chordFor, chordTooltip } from "../keyref";
import { chordKeycaps } from "../keys";
import { MONO_FONT } from "../monaco";
import { paneInstance } from "../panes";
import type { WorkbenchTool } from "../registry";
import { useStore } from "../store";
import { registerTerminal } from "../terminalInput";
import { attachRenderer } from "../terminalRenderer";
import { xtermTheme } from "../theme";
import type { TerminalClientMessage, TerminalServerMessage } from "../types";
import { wsUrl } from "../ws";

/**
 * The Terminal tool renders two ways, decided by the pane's id (`../panes.ts`).
 *
 *  - `terminal` — the default pane: a tab strip over every terminal that is not
 *    in a pane of its own. This is the panel Workbench has always had;
 *  - `terminal#<n>` — a pane bound to one terminal. No tab strip: the pane *is*
 *    the terminal, which is what a split is for.
 *
 * What `terminal#2` gets back after a reload is the pane and its number, with a
 * fresh shell — the PTY dies with the WebSocket and the server releases it, so
 * no arrangement on disk can promise the process.
 */
export function TerminalPanel(props: IDockviewPanelProps) {
  const paneKey = paneInstance(props.api.id);
  return paneKey === null ? (
    <TerminalTabs />
  ) : (
    <TerminalPane paneKey={paneKey} api={props.api} />
  );
}

/** One terminal, filling its pane. */
function TerminalPane({ paneKey, api }: { paneKey: string; api: DockviewPanelApi }) {
  const terminal = useStore((s) => s.terminals.find((t) => t.paneKey === paneKey));

  // Created on first sight of the pane — including a pane restored from a saved
  // layout, which is how "two terminals" comes back as two terminals.
  useEffect(() => {
    useStore.getState().ensureTerminalPane(paneKey);
  }, [paneKey]);

  // The focused terminal pane is the one a `shell` shortcut types into. Without
  // this the insert would go to whatever the tab strip last had active — text
  // delivered to a shell the user is not looking at.
  const id = terminal?.id ?? null;
  useEffect(() => {
    if (id === null) return;
    if (api.isActive) useStore.getState().setActiveTerminal(id);
    const subscription = api.onDidActiveChange((event) => {
      if (event.isActive) useStore.getState().setActiveTerminal(id);
    });
    return () => subscription.dispose();
  }, [api, id]);

  if (terminal === undefined) return null;
  return (
    <div className="wb-terminals">
      <div className="wb-terminals-body">
        <div className="wb-terminal" key={`${String(terminal.id)}:${String(terminal.generation)}`}>
          <TerminalInstance id={terminal.id} visible />
        </div>
      </div>
    </div>
  );
}

/**
 * N terminals, each with its own /ws/terminal socket and PTY. Every instance
 * stays mounted for the life of its tab — switching tabs only toggles CSS, so
 * scrollback, shell state and running processes survive. Closing a tab unmounts
 * it, which closes the socket and releases the PTY server-side.
 */
function TerminalTabs() {
  // Filtered *outside* the selector, not inside it: a selector that builds a
  // new array every call never compares equal, and zustand's
  // `useSyncExternalStore` re-renders forever on it (caught by running the app,
  // not by a type).
  const all = useStore((s) => s.terminals);
  // Terminals that live in a pane of their own are that pane's, not this
  // strip's: listing them here would show one shell in two places, each with
  // its own close button.
  const terminals = all.filter((t) => t.paneKey === null);
  const activeId = useStore((s) => s.activeTerminalId);

  return (
    <div className="wb-terminals">
      <div className="wb-term-tabs" role="tablist">
        {terminals.map((terminal) => (
          <div
            key={terminal.id}
            className={"wb-term-tab" + (terminal.id === activeId ? " is-active" : "")}
            role="tab"
            aria-selected={terminal.id === activeId}
          >
            <button
              type="button"
              className="wb-term-tab-label u-truncate"
              onClick={() => useStore.getState().setActiveTerminal(terminal.id)}
            >
              {terminal.alive && (
                <span
                  className="wb-dot wb-term-running"
                  role="img"
                  aria-label="Session running"
                  title="Session running"
                />
              )}
              {terminal.title}
            </button>
            <button
              type="button"
              className="wb-tab-close"
              aria-label={`Close ${terminal.title}`}
              onClick={() => useStore.getState().closeTerminal(terminal.id)}
            >
              ×
            </button>
          </div>
        ))}
        <button
          type="button"
          className="wb-term-add"
          aria-label="New terminal"
          // The chord comes from the registry, never from this string: a
          // tooltip that names a stale keycap teaches the wrong reflex
          // (DESIGN.md §6.13).
          title={chordTooltip("New terminal", "terminal.new")}
          onClick={() => useStore.getState().newTerminal()}
        >
          +
        </button>
      </div>
      <div className="wb-terminals-body">
        {terminals.length === 0 ? (
          <div className="wb-empty">
            <div className="wb-empty-title">No terminal</div>
            <div className="wb-empty-hint">
              New terminal —{" "}
              {chordKeycaps(chordFor("terminal.new")).map((cap) => (
                <span key={cap} className="wb-keycap">
                  {cap}
                </span>
              ))}
            </div>
          </div>
        ) : (
          terminals.map((terminal) => (
            <div
              key={`${terminal.id}:${terminal.generation}`}
              className={"wb-terminal" + (terminal.id === activeId ? "" : " is-hidden")}
            >
              <TerminalInstance id={terminal.id} visible={terminal.id === activeId} />
            </div>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * Fit only when the proposal is a usable size. A hidden or mid-layout host
 * measures 0, and fitting to that wedges the PTY at 1x1 — the server rejects
 * such a resize outright (cols/rows >= 2), which used to leave the terminal
 * blank with nothing on screen to explain why.
 */
function fitTerminal(fit: FitAddon): void {
  const dims = fit.proposeDimensions();
  if (dims !== undefined && dims.cols >= 2 && dims.rows >= 2) fit.fit();
}

/**
 * The host element, carrying a reader for what the terminal holds.
 *
 * On the GPU renderer xterm draws to a canvas and there is no `.xterm-rows` to
 * scrape — that element exists only under the DOM renderer. The reader hangs on
 * the host node rather than on `window` so that "the visible terminal" stays
 * expressed by the same CSS the E2E suite already selects with, and reads the
 * *buffer*: wrapped lines are rejoined, which the old `textContent` scrape could
 * not do (it split a shell line the shell considers unbroken, hiding markers
 * from a substring search).
 */
type TerminalHost = HTMLDivElement & { readTerminalText?: () => string };

function bufferText(term: XTerm): string {
  const buffer = term.buffer.active;
  const lines: string[] = [];
  for (let i = 0; i < buffer.length; i++) {
    const line = buffer.getLine(i);
    if (line === undefined) continue;
    const text = line.translateToString(true);
    if (line.isWrapped && lines.length > 0) lines[lines.length - 1] += text;
    else lines.push(text);
  }
  return lines.join("\n");
}

function TerminalInstance({ id, visible }: { id: number; visible: boolean }) {
  const theme = useStore((s) => s.theme);
  const [exited, setExited] = useState(false);
  const hostRef = useRef<TerminalHost>(null);
  const termRef = useRef<XTerm | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const mountedRef = useRef(false);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const term = new XTerm({
      fontFamily: MONO_FONT,
      fontSize: 13,
      lineHeight: 18 / 13,
      cursorStyle: "block",
      cursorBlink: false,
      scrollback: 5000,
      theme: xtermTheme(),
    });
    termRef.current = term;
    const fit = new FitAddon();
    fitRef.current = fit;
    term.loadAddon(fit);
    term.open(host);
    // After `open`, never before: the GPU renderer needs the element to exist.
    // Returns at once and finishes attaching later — the addon is a lazy chunk,
    // so the terminal is usable (on the DOM renderer) before it lands, and a
    // teardown that beats it leaves nothing attached.
    const renderer = attachRenderer(term);
    host.readTerminalText = () => bufferText(term);
    fitTerminal(fit);

    // Terminal sockets never auto-reconnect: the PTY behind them is stateful.
    const ws = new WebSocket(wsUrl("/ws/terminal"));
    const send = (message: TerminalClientMessage): boolean => {
      if (ws.readyState !== WebSocket.OPEN) return false;
      ws.send(JSON.stringify(message));
      return true;
    };
    const markExited = (): void => {
      setExited(true);
      useStore.getState().setTerminalAlive(id, false);
    };
    ws.onopen = () => send({ type: "resize", cols: term.cols, rows: term.rows });
    ws.onmessage = (ev: MessageEvent<string>) => {
      const message = JSON.parse(ev.data) as TerminalServerMessage;
      if (message.type === "output") term.write(message.data);
      else markExited();
    };
    ws.onclose = markExited;

    const dataSub = term.onData((data) => send({ type: "input", data }));
    const resizeSub = term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
    const observer = new ResizeObserver(() => fitTerminal(fit));
    observer.observe(host);
    // Shell shortcuts type through this handle — same path as a keystroke.
    const unregister = registerTerminal(id, {
      send: (data) => send({ type: "input", data }),
      focus: () => term.focus(),
    });

    return () => {
      unregister();
      observer.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      ws.onclose = null;
      ws.close();
      delete host.readTerminalText;
      renderer.dispose(); // before the terminal: the addon holds a GL context
      term.dispose();
      termRef.current = null;
      fitRef.current = null;
    };
  }, [id]);

  // Becoming visible: re-fit to the panel size it could not measure while hidden.
  //
  // Focus follows only a *switch* to this tab, never the first run: on launch the
  // restored terminal must not grab the keyboard, or the first Ctrl+P of the
  // session passes through to the shell (pass-through policy, keys.ts) and any
  // typed text lands in a PTY the user never meant to touch.
  useEffect(() => {
    const first = !mountedRef.current;
    mountedRef.current = true;
    if (!visible) return;
    const fit = fitRef.current;
    if (fit !== null) fitTerminal(fit);
    if (!first) termRef.current?.focus();
  }, [visible]);

  useEffect(() => {
    const term = termRef.current;
    if (term) term.options.theme = xtermTheme();
  }, [theme]);

  return (
    <>
      <div ref={hostRef} className="wb-terminal-host" />
      {exited && (
        <div className="wb-terminal-exited">
          <div className="wb-terminal-exited-card">
            <div className="wb-empty-title">Terminal exited</div>
            <button
              type="button"
              className="wb-btn wb-btn-outline"
              onClick={() => useStore.getState().restartTerminal(id)}
            >
              Reconnect
            </button>
          </div>
        </div>
      )}
    </>
  );
}

// ---- registration -----------------------------------------------------------

export const terminalTool: WorkbenchTool = {
  id: "terminal",
  title: "Terminal",
  panel: {
    component: TerminalPanel,
    defaultLocation: { area: "bottom", size: 260 },
    // Plural: a terminal belongs in a pane as readily as in a tab.
    singleton: false,
    instances: {
      // One row, and it always makes a new shell: unlike a session or a file, a
      // terminal is not something that exists before you ask for it. The key is
      // the lowest free pane number rather than a counter, so it survives the
      // reload that resets every counter in the app (`store.ts`).
      options: () => [
        {
          id: "terminal.new",
          title: "New terminal",
          detail: "a shell in its own pane",
          category: "Terminal",
          key: () => useStore.getState().nextTerminalPaneKey(),
        },
      ],
      titleFor: (key) => `Terminal ${key}`,
    },
  },
  // A `shell` shortcut is typed into the active terminal, so this panel comes
  // forward first — declared here so `commands.ts` routes by capability.
  shortcutKinds: ["shell"],
  commands: [
    {
      id: "terminal.new",
      title: "New terminal",
      run: () => useStore.getState().newTerminal(),
    },
    {
      id: "terminal.close",
      title: "Close terminal",
      when: () => useStore.getState().terminals.length > 0,
      run: () => {
        const s = useStore.getState();
        if (s.activeTerminalId !== null) s.closeTerminal(s.activeTerminalId);
      },
    },
  ],
  shortcuts: { "terminal.new": ["Alt+T"] },
};
