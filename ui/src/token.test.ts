/**
 * The per-launch token handshake (`token.ts`).
 *
 * Two ways in: the shell hands it over (`authToken()` non-null), or a browser
 * tab fetches `GET /api/auth/token`. Both are mocked here — `authToken` via a
 * module mock, `fetch` via a global stub — and the module's own state is reset
 * between tests with `vi.resetModules()` + a fresh dynamic import.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { authTokenMock } = vi.hoisted(() => ({
  authTokenMock: vi.fn<() => Promise<string | null>>(),
}));
vi.mock("./shell", () => ({ authToken: authTokenMock }));

async function loadToken(): Promise<typeof import("./token")> {
  vi.resetModules();
  return import("./token");
}

const okJson = (body: unknown): Response =>
  ({ ok: true, json: () => Promise.resolve(body) }) as unknown as Response;

beforeEach(() => {
  authTokenMock.mockReset();
  authTokenMock.mockResolvedValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("initToken (browser path)", () => {
  it("fetches the handshake and stores the token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ token: "T-browser" }));
    vi.stubGlobal("fetch", fetchMock);

    const { initToken, getToken } = await loadToken();
    expect(getToken()).toBeNull();
    await initToken();

    expect(getToken()).toBe("T-browser");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("never puts the token in a URL or query string", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okJson({ token: "secret" }));
    vi.stubGlobal("fetch", fetchMock);

    const { initToken } = await loadToken();
    await initToken();

    // Same-origin, path only — no query, no token in the request line.
    expect(fetchMock).toHaveBeenCalledWith("/api/auth/token");
  });

  it("leaves the token null when the handshake fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false } as Response));

    const { initToken, getToken } = await loadToken();
    await initToken();

    expect(getToken()).toBeNull();
  });

  it("swallows a thrown fetch and leaves the token null", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));

    const { initToken, getToken } = await loadToken();
    await expect(initToken()).resolves.toBeUndefined();
    expect(getToken()).toBeNull();
  });
});

describe("initToken (shell path)", () => {
  it("takes the token from the shell and skips the handshake", async () => {
    authTokenMock.mockResolvedValue("T-shell");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const { initToken, getToken } = await loadToken();
    await initToken();

    expect(getToken()).toBe("T-shell");
    expect(fetchMock, "the shell answered, so no HTTP handshake").not.toHaveBeenCalled();
  });
});
