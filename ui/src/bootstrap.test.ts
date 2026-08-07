/**
 * The launch invariant `App.tsx` enforces: the per-launch token is fetched
 * (`initToken`) before `init()` opens the first REST call or socket, so every
 * one of them carries it. App gates `init()` on `tokenReady`; this proves the
 * same guarantee at the real seam — `token.ts`, `api.ts` and `ws.ts` wired
 * together with nothing mocked but the network — which a node test environment
 * can exercise without a React DOM (`vitest.config.ts` is node-only by design).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const okJson = (body: unknown): Response =>
  ({ ok: true, json: () => Promise.resolve(body) }) as unknown as Response;

async function loadSeam(): Promise<
  typeof import("./token") & typeof import("./api") & typeof import("./ws")
> {
  vi.resetModules();
  const token = await import("./token");
  const api = await import("./api");
  const ws = await import("./ws");
  return { ...token, ...api, ...ws };
}

const tokenHeader = (fetchMock: ReturnType<typeof vi.fn>, url: string): string | null => {
  const call = fetchMock.mock.calls.find((c) => c[0] === url);
  if (call === undefined) throw new Error(`no fetch to ${url}`);
  return new Headers((call[1] as RequestInit).headers).get("X-Workbench-Token");
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("launch token ordering", () => {
  it("a REST call before the handshake carries no token, and no socket does", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    const { getUsage, wsProtocols } = await loadSeam();
    await getUsage();

    expect(tokenHeader(fetchMock, "/api/usage")).toBeNull();
    expect(wsProtocols()).toEqual([]);
  });

  it("once initToken has resolved, the REST call carries the token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okJson({ token: "T-boot" })) // the handshake
      .mockResolvedValue(okJson({})); // the REST call init() would make
    vi.stubGlobal("fetch", fetchMock);

    const { initToken, getUsage, wsProtocols } = await loadSeam();
    // App awaits this before it ever calls init().
    await initToken();
    await getUsage();

    expect(tokenHeader(fetchMock, "/api/usage")).toBe("T-boot");
    // And every socket opened after the handshake offers the same token as its
    // `workbench.auth.<token>` subprotocol (`OFFER_WS_TOKEN`, ws.ts) — the WS
    // transport for the token, which the server echoes on accept.
    expect(wsProtocols()).toEqual(["workbench.auth.T-boot"]);
  });
});
