import { FitAddon } from "@xterm/addon-fit";
import { Terminal as XTerm } from "@xterm/xterm";
import type { IDockviewPanelProps } from "dockview";
import { useEffect, useRef, useState } from "react";

import { MONO_FONT } from "../monaco";
import { useStore } from "../store";
import { xtermTheme } from "../theme";
import type { TerminalClientMessage, TerminalServerMessage } from "../types";
import { wsUrl } from "../ws";

export function TerminalPanel(_props: IDockviewPanelProps) {
  const generation = useStore((s) => s.terminalGeneration);
  // Remounting on generation change tears down the old PTY socket and opens a
  // fresh one — this is both "Reconnect" and the QuickBar "New terminal" action.
  return <TerminalInstance key={generation} />;
}

function TerminalInstance() {
  const theme = useStore((s) => s.theme);
  const [exited, setExited] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<XTerm | null>(null);

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
    term.loadAddon(fit);
    term.open(host);
    fit.fit();

    // Terminal sockets never auto-reconnect: the PTY behind them is stateful.
    const ws = new WebSocket(wsUrl("/ws/terminal"));
    const send = (message: TerminalClientMessage): void => {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(message));
    };
    ws.onopen = () => send({ type: "resize", cols: term.cols, rows: term.rows });
    ws.onmessage = (ev: MessageEvent<string>) => {
      const message = JSON.parse(ev.data) as TerminalServerMessage;
      if (message.type === "output") term.write(message.data);
      else setExited(true);
    };
    ws.onclose = () => setExited(true);

    const dataSub = term.onData((data) => send({ type: "input", data }));
    const resizeSub = term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
    const observer = new ResizeObserver(() => fit.fit());
    observer.observe(host);

    return () => {
      observer.disconnect();
      dataSub.dispose();
      resizeSub.dispose();
      ws.onclose = null;
      ws.close();
      term.dispose();
      termRef.current = null;
    };
  }, []);

  useEffect(() => {
    const term = termRef.current;
    if (term) term.options.theme = xtermTheme();
  }, [theme]);

  return (
    <div className="wb-terminal">
      <div ref={hostRef} className="wb-terminal-host" />
      {exited && (
        <div className="wb-terminal-exited">
          <div className="wb-terminal-exited-card">
            <div className="wb-empty-title">Terminal exited</div>
            <button
              type="button"
              className="wb-btn wb-btn-outline"
              onClick={() => useStore.getState().newTerminal()}
            >
              Reconnect
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
