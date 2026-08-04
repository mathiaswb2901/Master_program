import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import type { IDockviewPanelProps } from "dockview";
import { useEffect, useRef, useState } from "react";

import { MONO_FONT } from "../monaco";
import { useStore } from "../store";
import { xtermTheme } from "../theme";
import type { TerminalClientMessage, TerminalServerMessage } from "../types";
import { wsUrl } from "../ws";

/**
 * N terminals, each with its own /ws/terminal socket and PTY. Every instance
 * stays mounted for the life of its tab — switching tabs only toggles CSS, so
 * scrollback, shell state and running processes survive. Closing a tab unmounts
 * it, which closes the socket and releases the PTY server-side.
 */
export function TerminalPanel(_props: IDockviewPanelProps) {
  const terminals = useStore((s) => s.terminals);
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
          title="New terminal — Alt+T"
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
              New terminal — <span className="wb-keycap">Alt</span>{" "}
              <span className="wb-keycap">T</span>
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

function TerminalInstance({ id, visible }: { id: number; visible: boolean }) {
  const theme = useStore((s) => s.theme);
  const [exited, setExited] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
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
    fitTerminal(fit);

    // Terminal sockets never auto-reconnect: the PTY behind them is stateful.
    const ws = new WebSocket(wsUrl("/ws/terminal"));
    const send = (message: TerminalClientMessage): void => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
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

    return () => {
      observer.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      ws.onclose = null;
      ws.close();
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
