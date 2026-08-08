/**
 * The backend-origin seam (`backend.ts`): where the UI turns a `/api` or `/ws`
 * path into a URL it can reach.
 *
 * The whole bug this fixes: a built desktop bundle serves the UI from the asset
 * protocol, which is not same-origin with the Python backend, so a relative
 * `/api/x` never reaches it. Two hosts, one seam — a browser stays same-origin
 * (unchanged), the shell targets the explicit `127.0.0.1:<port>` it spawned the
 * backend on. And it must compose with the auth token, not replace it.
 *
 * `./shell` is mocked so this drives the host decision directly; the module
 * caches its resolved origin, so each test re-imports it fresh.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { backendOriginMock } = vi.hoisted(() => ({
  backendOriginMock: vi.fn<() => Promise<string | null>>(),
}));
vi.mock("./shell", () => ({ backendOrigin: backendOriginMock }));

const globals = globalThis as unknown as Record<string, unknown>;

beforeEach(() => {
  vi.resetModules();
  backendOriginMock.mockReset();
});

afterEach(() => {
  delete globals.location;
  vi.unstubAllGlobals();
});

/** A fresh copy of the seam, with its origin cache reset. */
async function freshBackend(): Promise<typeof import("./backend")> {
  return await import("./backend");
}

describe("browser host (same-origin, unchanged)", () => {
  it("keeps REST paths relative and derives ws:// from location", async () => {
    // A browser tab: the shell command returns null (it is a no-op off Tauri).
    backendOriginMock.mockResolvedValue(null);
    globals.location = { protocol: "http:", host: "workbench.test" };

    const backend = await freshBackend();
    await backend.initBackendOrigin();

    expect(backend.backendHttpOrigin()).toBe("");
    expect(backend.apiUrl("/api/usage")).toBe("/api/usage");
    expect(backend.wsUrl("/ws/events")).toBe("ws://workbench.test/ws/events");
  });

  it("upgrades to wss:// on an https page", async () => {
    backendOriginMock.mockResolvedValue(null);
    globals.location = { protocol: "https:", host: "workbench.test" };

    const backend = await freshBackend();
    await backend.initBackendOrigin();

    expect(backend.wsUrl("/ws/events")).toBe("wss://workbench.test/ws/events");
  });

  it("stays same-origin even if the seam is never initialised", async () => {
    globals.location = { protocol: "http:", host: "workbench.test" };
    const backend = await freshBackend();
    // No initBackendOrigin call at all — the default must be same-origin.
    expect(backend.apiUrl("/api/x")).toBe("/api/x");
    expect(backend.wsUrl("/ws/x")).toBe("ws://workbench.test/ws/x");
  });
});

describe("shell host (explicit 127.0.0.1:port)", () => {
  it("prefixes REST calls and points sockets at the backend port", async () => {
    // The shell hands over the origin it spawned the backend on.
    backendOriginMock.mockResolvedValue("http://127.0.0.1:9000");

    const backend = await freshBackend();
    await backend.initBackendOrigin();

    expect(backend.backendHttpOrigin()).toBe("http://127.0.0.1:9000");
    expect(backend.apiUrl("/api/usage")).toBe("http://127.0.0.1:9000/api/usage");
    // ws, not the asset protocol — this is the line the bug turned into a dead UI.
    expect(backend.wsUrl("/ws/events")).toBe("ws://127.0.0.1:9000/ws/events");
  });

  it("treats a shell that could not answer like a browser", async () => {
    // shell.ts already swallows its IPC failure and returns null; a null answer
    // must leave the origin same-origin, never crash the seam.
    backendOriginMock.mockResolvedValue(null);
    globals.location = { protocol: "http:", host: "tauri.localhost" };

    const backend = await freshBackend();
    await backend.initBackendOrigin();

    expect(backend.backendHttpOrigin()).toBe("");
    expect(backend.apiUrl("/api/x")).toBe("/api/x");
  });
});

describe("composes with the auth token", () => {
  const okJson = (body: unknown): Response =>
    ({ ok: true, json: () => Promise.resolve(body) }) as unknown as Response;

  it("attaches the token header AND the shell origin on the same call", async () => {
    // Both mocked into the same fresh module graph so api.ts reads this seam.
    vi.doMock("./token", () => ({ getToken: () => "T-shell" }));
    backendOriginMock.mockResolvedValue("http://127.0.0.1:9000");

    const backend = await freshBackend();
    await backend.initBackendOrigin();
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    const { getUsage } = await import("./api");
    await getUsage();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:9000/api/usage");
    expect(new Headers(init.headers).get("X-Workbench-Token")).toBe("T-shell");
    vi.doUnmock("./token");
  });

  it("in a browser keeps the relative URL AND the token header", async () => {
    vi.doMock("./token", () => ({ getToken: () => "T-browser" }));
    backendOriginMock.mockResolvedValue(null);

    const backend = await freshBackend();
    await backend.initBackendOrigin();
    const fetchMock = vi.fn().mockResolvedValue(okJson({}));
    vi.stubGlobal("fetch", fetchMock);

    const { getUsage } = await import("./api");
    await getUsage();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/usage");
    expect(new Headers(init.headers).get("X-Workbench-Token")).toBe("T-browser");
    vi.doUnmock("./token");
  });
});
