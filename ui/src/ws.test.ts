/**
 * `ReconnectingSocket`: retry forever on a blip, never on a refusal.
 *
 * The distinction is the whole point. A backend restarting, a laptop waking, a
 * dropped Wi-Fi frame — all transient, and reconnecting through them is why
 * this class exists. A close code in the 4400s is the server saying the thing
 * behind this path does not exist and will not start existing, which is what
 * `/ws/agent/{id}` answers (4404) for a session id it does not have. Retrying
 * that is a request every ten seconds for the life of the tab, per socket, with
 * nothing on screen to explain it.
 *
 * Node environment: `WebSocket`, `location` and `window` are stubbed here
 * rather than pulling in a DOM stack, which is the pattern `test-setup.ts`
 * describes.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { getTokenMock } = vi.hoisted(() => ({ getTokenMock: vi.fn<() => string | null>() }));
vi.mock("./token", () => ({ getToken: getTokenMock }));

import { ReconnectingSocket, wsProtocols, wsTokenProtocol } from "./ws";

interface CloseLike {
  code: number;
}

/** Every socket the class asked for, in order — the assertion surface. */
let sockets: FakeSocket[] = [];

class FakeSocket {
  static readonly OPEN = 1;
  readyState = 0;
  readonly sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: ((ev: CloseLike) => void) | null = null;

  // The real WebSocket takes an optional subprotocol arg; the token rides it.
  constructor(
    readonly url: string,
    readonly protocols?: string | string[],
  ) {
    sockets.push(this);
  }

  send(frame: string): void {
    this.sent.push(frame);
  }

  close(): void {
    this.readyState = 3;
  }

  /** The server accepted it. */
  accept(): void {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  /** The server (or the network) ended it. */
  ended(code: number): void {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

const globals = globalThis as unknown as Record<string, unknown>;

beforeEach(() => {
  sockets = [];
  vi.useFakeTimers();
  globals.WebSocket = FakeSocket;
  globals.location = { protocol: "http:", host: "workbench.test" };
  // `window.setTimeout` is the faked global here, so backoff is advanceable.
  globals.window = globalThis;
  // No token by default: the socket tests below assert reconnect behaviour, not
  // auth, and an empty protocol list is exactly what "no token yet" produces.
  getTokenMock.mockReset();
  getTokenMock.mockReturnValue(null);
});

afterEach(() => {
  vi.useRealTimers();
  delete globals.WebSocket;
  delete globals.location;
  delete globals.window;
});

const latest = (): FakeSocket => {
  const socket = sockets.at(-1);
  if (socket === undefined) throw new Error("no socket was opened");
  return socket;
};

describe("ReconnectingSocket", () => {
  it("connects to the ws:// form of the path", () => {
    new ReconnectingSocket("/ws/agent/abc", { onMessage: () => undefined });
    expect(sockets).toHaveLength(1);
    expect(latest().url).toBe("ws://workbench.test/ws/agent/abc");
  });

  it("reconnects after a transient close, with backoff", () => {
    new ReconnectingSocket("/ws/events", { onMessage: () => undefined });
    latest().ended(1006); // the code a killed process produces

    expect(sockets, "no reconnect before the backoff elapses").toHaveLength(1);
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);

    latest().ended(1006);
    vi.advanceTimersByTime(500);
    expect(sockets, "second gap is longer than the first").toHaveLength(2);
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(3);
  });

  it("stops for good when the server refuses the path", () => {
    const gone = vi.fn();
    new ReconnectingSocket("/ws/agent/dead0cafe911", {
      onMessage: () => undefined,
      onGone: gone,
    });
    latest().ended(4404); // "unknown session" — routers/agents.py

    expect(gone).toHaveBeenCalledWith(4404);
    // An hour of a tab left open is six retries at the 10 s cap, per pane.
    vi.advanceTimersByTime(3_600_000);
    expect(sockets, "never reconnected").toHaveLength(1);
  });

  it("keeps retrying codes outside the refusal band", () => {
    new ReconnectingSocket("/ws/agent/abc", { onMessage: () => undefined });
    latest().ended(4500);
    vi.advanceTimersByTime(500);
    expect(sockets).toHaveLength(2);
  });

  it("queues frames until the socket opens, and drops them once it is gone", () => {
    const socket = new ReconnectingSocket("/ws/agent/abc", { onMessage: () => undefined });
    socket.send({ type: "user_message", text: "hi" });
    expect(latest().sent, "queued while connecting").toEqual([]);

    latest().accept();
    expect(latest().sent).toEqual(['{"type":"user_message","text":"hi"}']);

    latest().ended(4404);
    socket.send({ type: "user_message", text: "anyone there" });
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
    expect(latest().sent, "nothing queued for a socket that will never open").toHaveLength(1);
  });

  it("close() stops the retry loop too", () => {
    const socket = new ReconnectingSocket("/ws/events", { onMessage: () => undefined });
    socket.close();
    latest().ended(1006);
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);
  });

  it("delivers parsed frames and ignores unparseable ones", () => {
    const seen: unknown[] = [];
    new ReconnectingSocket("/ws/events", { onMessage: (data) => seen.push(data) });
    latest().onmessage?.({ data: '{"type":"ping"}' });
    latest().onmessage?.({ data: "not json" });
    expect(seen).toEqual([{ type: "ping" }]);
  });

  it("opens every socket with the subprotocol list wsProtocols() decides", () => {
    // Gated off today (the server does not echo the label yet), so the list is
    // empty and a reconnect re-reads the same empty list — the socket is still
    // wired through wsProtocols(), which is what flips when the server echoes.
    getTokenMock.mockReturnValue("T-sock");
    new ReconnectingSocket("/ws/events", { onMessage: () => undefined });
    expect(latest().protocols).toEqual([]);

    latest().ended(1006);
    vi.advanceTimersByTime(500);
    expect(latest().protocols).toEqual([]);
  });
});

describe("wsTokenProtocol", () => {
  it("builds the workbench.auth.<token> label", () => {
    expect(wsTokenProtocol("abc123")).toBe("workbench.auth.abc123");
  });
});

describe("wsProtocols", () => {
  // Held off until the server echoes the subprotocol: a browser fails a
  // handshake whose offered subprotocol is not echoed back, and the WS
  // endpoints accept() with none today. So the offer is empty even with a
  // token — the REST path carries it instead (api.ts). Flipping OFFER_WS_TOKEN
  // on is the WS half of enabling enforcement (PR4), paired with a server echo.
  it("does not offer the token while the server does not echo it", () => {
    getTokenMock.mockReturnValue("abc123");
    expect(wsProtocols()).toEqual([]);
  });

  it("is empty when no token has resolved", () => {
    getTokenMock.mockReturnValue(null);
    expect(wsProtocols()).toEqual([]);
  });
});
