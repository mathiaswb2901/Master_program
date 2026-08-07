/**
 * The REST client attaches the per-launch token as a header on every call —
 * one merge in `request()` covers all of them (`api.ts`). The token source
 * (`./token`) is mocked so this asserts only the header wiring.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

const { getTokenMock } = vi.hoisted(() => ({ getTokenMock: vi.fn<() => string | null>() }));
vi.mock("./token", () => ({ getToken: getTokenMock }));

import { getUsage } from "./api";

const okJson = (body: unknown): Response =>
  ({ ok: true, json: () => Promise.resolve(body) }) as unknown as Response;

/** The Headers the one and only fetch was called with. */
function sentHeaders(fetchMock: ReturnType<typeof vi.fn>): Headers {
  const init = fetchMock.mock.calls[0][1] as RequestInit;
  return new Headers(init.headers);
}

afterEach(() => {
  vi.unstubAllGlobals();
  getTokenMock.mockReset();
});

describe("request() token header", () => {
  it("sends X-Workbench-Token when a token is present", async () => {
    getTokenMock.mockReturnValue("T-123");
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    await getUsage();

    expect(sentHeaders(fetchMock).get("X-Workbench-Token")).toBe("T-123");
  });

  it("omits the header when no token has resolved yet", async () => {
    getTokenMock.mockReturnValue(null);
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    await getUsage();

    expect(sentHeaders(fetchMock).has("X-Workbench-Token")).toBe(false);
  });

  it("keeps the caller's own headers alongside the token", async () => {
    getTokenMock.mockReturnValue("T-123");
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    // A JSON PUT carries Content-Type; the token must not clobber it.
    const { putLayouts } = await import("./api");
    await putLayouts({ layouts: [], active: null } as never);

    const headers = sentHeaders(fetchMock);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("X-Workbench-Token")).toBe("T-123");
  });
});
