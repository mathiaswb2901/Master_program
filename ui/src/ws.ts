/**
 * Typed WebSocket helpers. ReconnectingSocket auto-reconnects with backoff and
 * queues outbound frames while disconnected — used for /ws/events and /ws/agent/{id}.
 * The terminal deliberately does NOT reconnect (a PTY is stateful); the Terminal
 * panel manages its own raw WebSocket.
 */

import { getToken } from "./token";

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

/** The subprotocol label that carries the token: `workbench.auth.<token>`.
 * Mirrors `WS_TOKEN_SUBPROTOCOL_PREFIX` in `services/local_auth.py`. */
const WS_TOKEN_PREFIX = "workbench.auth.";

/**
 * Build the token-bearing WebSocket subprotocol. A browser cannot set headers
 * on a `new WebSocket`, so the token travels as a subprotocol — the one
 * client-controlled value a WS handshake carries, and never a URL/query string.
 * Pure; whether it is actually offered is decided by `wsProtocols`.
 */
export function wsTokenProtocol(token: string): string {
  return `${WS_TOKEN_PREFIX}${token}`;
}

/**
 * Whether a browser socket OFFERS the token subprotocol.
 *
 * ON (PR4). A browser *fails* any WebSocket whose offered subprotocol the
 * server does not echo back in the 101 response, so this pairs one-to-one with
 * the server-side `accept(subprotocol=…)` that echoes the label on every WS
 * endpoint (`events`, `agents`, `office_host`, `terminal` — `ws_subprotocol_to_echo`
 * in `services/local_auth.py`). With enforcement on the middleware also reads
 * the token from this same offered subprotocol to authenticate the handshake, so
 * offering it is what lets a browser socket connect at all. The REST token rides
 * a header instead (`api.ts`) — a `fetch` can set headers; a `new WebSocket`
 * cannot, which is why the socket carries it as a subprotocol.
 */
const OFFER_WS_TOKEN = true;

/**
 * The subprotocol list every socket opens with: the token once it may be
 * offered, otherwise empty — which a browser treats exactly like offering no
 * subprotocol (verified: `new WebSocket(url, [])` opens). Used by
 * `ReconnectingSocket` below (/ws/events, /ws/agent, /ws/office-host) and the
 * terminal's one raw socket (`Terminal.tsx`).
 */
export function wsProtocols(): string[] {
  const token = getToken();
  if (!OFFER_WS_TOKEN || token === null) return [];
  return [wsTokenProtocol(token)];
}

/**
 * Close codes in this band mean the server **refused** the connection and
 * always will: the thing behind that path does not exist. Retrying then is not
 * optimism about a flaky network, it is one request every ten seconds for as
 * long as the tab is open, invisible to the user.
 *
 * `/ws/agent/{id}` answers 4404 for a session id the server does not have
 * (`routers/agents.py`) — which is exactly what a saved layout asks for after
 * the server restarts, because `SessionManager` holds its sessions in memory.
 *
 * Everything outside the band — a dropped network, a backend restarting, the
 * 1006 a killed process produces — is transient and still retried forever,
 * which is the whole reason this class exists.
 */
const GONE_CODE_MIN = 4400;
const GONE_CODE_MAX = 4499;

export interface ReconnectingSocketOptions {
  onMessage: (data: unknown) => void;
  /** Fires on every (re)connect — use to re-sync state missed while offline. */
  onOpen?: () => void;
  /** The server refused this path for good; the socket has stopped retrying. */
  onGone?: (code: number) => void;
}

export class ReconnectingSocket {
  /** No further connection will ever be made: closed by us, or refused for good. */
  private disposed = false;
  private ws: WebSocket | null = null;
  private attempts = 0;
  private queue: string[] = [];

  constructor(
    private readonly path: string,
    private readonly opts: ReconnectingSocketOptions,
  ) {
    this.connect();
  }

  private connect(): void {
    const ws = new WebSocket(wsUrl(this.path), wsProtocols());
    this.ws = ws;
    ws.onopen = () => {
      this.attempts = 0;
      for (const frame of this.queue.splice(0)) ws.send(frame);
      this.opts.onOpen?.();
    };
    ws.onmessage = (ev: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(ev.data);
      } catch {
        return;
      }
      this.opts.onMessage(parsed);
    };
    ws.onclose = (ev: CloseEvent) => {
      if (this.disposed) return;
      if (ev.code >= GONE_CODE_MIN && ev.code <= GONE_CODE_MAX) {
        this.disposed = true;
        this.queue.length = 0;
        this.opts.onGone?.(ev.code);
        return;
      }
      const delay = Math.min(10_000, 500 * 2 ** this.attempts);
      this.attempts += 1;
      window.setTimeout(() => {
        if (!this.disposed) this.connect();
      }, delay);
    };
  }

  send(message: unknown): void {
    // Queueing for a socket that will never open again is a leak, not patience.
    if (this.disposed) return;
    const frame = JSON.stringify(message);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(frame);
    else this.queue.push(frame);
  }

  close(): void {
    this.disposed = true;
    this.ws?.close();
  }
}
