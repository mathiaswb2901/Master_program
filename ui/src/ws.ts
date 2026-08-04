/**
 * Typed WebSocket helpers. ReconnectingSocket auto-reconnects with backoff and
 * queues outbound frames while disconnected — used for /ws/events and /ws/agent/{id}.
 * The terminal deliberately does NOT reconnect (a PTY is stateful); the Terminal
 * panel manages its own raw WebSocket.
 */

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}

export interface ReconnectingSocketOptions {
  onMessage: (data: unknown) => void;
  /** Fires on every (re)connect — use to re-sync state missed while offline. */
  onOpen?: () => void;
}

export class ReconnectingSocket {
  private ws: WebSocket | null = null;
  private disposed = false;
  private attempts = 0;
  private queue: string[] = [];

  constructor(
    private readonly path: string,
    private readonly opts: ReconnectingSocketOptions,
  ) {
    this.connect();
  }

  private connect(): void {
    const ws = new WebSocket(wsUrl(this.path));
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
    ws.onclose = () => {
      if (this.disposed) return;
      const delay = Math.min(10_000, 500 * 2 ** this.attempts);
      this.attempts += 1;
      window.setTimeout(() => {
        if (!this.disposed) this.connect();
      }, delay);
    };
  }

  send(message: unknown): void {
    const frame = JSON.stringify(message);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(frame);
    else this.queue.push(frame);
  }

  close(): void {
    this.disposed = true;
    this.ws?.close();
  }
}
