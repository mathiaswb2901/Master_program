import { afterEach, describe, expect, it, vi } from "vitest";

// The relay resolves commands against the *real* registry (that is the point —
// one dispatch, not a second), so this drags in the real panel modules exactly
// as `commands.test.ts` does, and stubs the store and Monaco for the same
// reason. `./api` and `./ws` are stubbed so nothing here opens a socket or hits
// the network — this suite is the executor's decision logic, not its plumbing.
const mocks = vi.hoisted(() => ({
  state: {
    theme: "dark",
    activePath: null,
    openFiles: [] as unknown[],
    folders: [] as unknown[],
    terminals: [] as unknown[],
    activeTerminalId: null,
    shortcuts: [] as unknown[],
    runShortcut: () => undefined,
    pushToast: vi.fn(),
    toggleTheme: vi.fn(),
    setQuickBarOpen: vi.fn(),
  },
}));

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => mocks.state }),
  emptyPlanDraft: () => ({
    choices: {},
    notes: {},
    comment: "",
    verdict: null,
    annotating: false,
    editing: null,
  }),
  noteText: () => "",
  pendingPlanId: () => null,
  unchosenOptionGroups: () => [],
}));

const apiMocks = vi.hoisted(() => ({
  publishCommandManifest: vi.fn(() => Promise.resolve({ ok: true })),
  reportCommandResult: vi.fn(() => Promise.resolve({ ok: true })),
}));

vi.mock("./api", () => apiMocks);

// A ReconnectingSocket stub: records the handlers and whether it was closed, so
// the lifecycle tests can drive open/close without a real WebSocket.
const socketMocks = vi.hoisted(() => ({
  instances: [] as { close: ReturnType<typeof vi.fn> }[],
}));

vi.mock("./ws", () => ({
  ReconnectingSocket: class {
    close = vi.fn();
    constructor(_url: string, _opts: unknown) {
      socketMocks.instances.push(this);
    }
  },
}));

vi.mock("./monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  loadMonaco: () => Promise.resolve({}),
  prefetchMonaco: () => undefined,
}));

const { buildManifest, executeCommandById, invocableCommands, startCommandRelay, stopCommandRelay } =
  await import("./commandRelay");
const { builtinCommands } = await import("./commands");

describe("the command relay's window half", () => {
  it("publishes a manifest of safe, registered commands", () => {
    const ids = new Set(invocableCommands(builtinCommands()).map((c) => c.id));
    // A safe global command is invocable…
    expect(ids.has("view.toggleTheme")).toBe(true);
    // …and one that re-points the path jail is not (item 4 / isBindableFromFile).
    expect(ids.has("workspace.open")).toBe(false);
    // Every published item carries the wire shape.
    const item = buildManifest(builtinCommands()).commands.find((c) => c.id === "view.toggleTheme");
    expect(item).toEqual({
      id: "view.toggleTheme",
      title: expect.any(String),
      takes_params: false,
    });
  });

  it("runs a registered command through the registry's own dispatch", () => {
    const outcome = executeCommandById("view.toggleTheme", builtinCommands());
    expect(outcome.ok).toBe(true);
    // The one dispatch, not a second: running the command reached the store.
    expect(mocks.state.toggleTheme).toHaveBeenCalledTimes(1);
  });

  it("errors on an unknown id without running anything", () => {
    const outcome = executeCommandById("definitely.not.a.command", builtinCommands());
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("no command");
  });

  it("refuses a command that opts out of external invocation", () => {
    const outcome = executeCommandById("workspace.open", builtinCommands());
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("cannot be invoked");
  });
});

describe("the relay's teardown", () => {
  afterEach(() => {
    stopCommandRelay();
    apiMocks.publishCommandManifest.mockClear();
    socketMocks.instances.length = 0;
  });

  it("unpublishes the manifest and closes the socket when stopped", () => {
    startCommandRelay();
    const socket = socketMocks.instances.at(-1);
    apiMocks.publishCommandManifest.mockClear(); // ignore the on-connect publish

    stopCommandRelay();

    // The empty manifest is what makes the backend say "no window connected"
    // again, instead of leaving this window's stale commands listed.
    expect(apiMocks.publishCommandManifest).toHaveBeenCalledWith({ commands: [] });
    expect(socket?.close).toHaveBeenCalledTimes(1);
  });

  it("does not unpublish when it was never running", () => {
    stopCommandRelay();
    expect(apiMocks.publishCommandManifest).not.toHaveBeenCalled();
  });
});
