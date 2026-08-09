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
    // `session.start`'s three store calls, so the parameterised path can be
    // driven all the way to "the prompt reached the session it created".
    createSessionIn: vi.fn(() => Promise.resolve("session-1")),
    focusSession: vi.fn(),
    sendChat: vi.fn(),
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
    // …and one that re-points the path jail is not, as a *gesture* — it is
    // published only because its parameters narrow it (see the next test).
    expect(ids.has("workspace.switch")).toBe(false);
    // Every published item carries the wire shape.
    const item = buildManifest(builtinCommands()).commands.find((c) => c.id === "view.toggleTheme");
    expect(item).toEqual({
      id: "view.toggleTheme",
      title: expect.any(String),
      takes_params: false,
    });
  });

  it("publishes a parameterised command with its schema", () => {
    const item = buildManifest(builtinCommands()).commands.find((c) => c.id === "layout.switch");
    expect(item?.takes_params).toBe(true);
    expect(item?.params_schema).toEqual({
      params: [
        {
          name: "name",
          type: "string",
          required: true,
          max_length: null,
          detail: "a layout this window has",
        },
      ],
    });
    // …and a parameterless one publishes no schema at all, which is the whole
    // reason the manifest still fits inside run_command's result budget.
    const plain = buildManifest(builtinCommands()).commands.find((c) => c.id === "panel.terminal");
    expect(plain?.params_schema).toBeUndefined();
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
    const outcome = executeCommandById("workspace.switch", builtinCommands());
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("cannot be invoked");
  });

  it("refuses the bare gesture of a command admitted only by its parameters", () => {
    // `workspace.open` with no arguments opens a folder dialog onto the whole
    // filesystem. It is published *because* `{path}` narrows it to the recent
    // list, so an argument-less invoke is the gesture wearing a schema.
    const outcome = executeCommandById("workspace.open", builtinCommands());
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("may only be invoked from outside with: path");
  });

  it("refuses arguments a command did not declare, and says what it takes", () => {
    const unknown = executeCommandById("layout.switch", builtinCommands(), { nmae: "Review" });
    expect(unknown.ok).toBe(false);
    expect(unknown.detail).toContain("no parameter “nmae”");
    expect(unknown.detail).toContain("takes: name");

    const missing = executeCommandById("layout.switch", builtinCommands(), {});
    expect(missing.ok).toBe(false);
    expect(missing.detail).toContain("needs “name”");

    const wrongType = executeCommandById("layout.switch", builtinCommands(), { name: 7 });
    expect(wrongType.ok).toBe(false);
    expect(wrongType.detail).toContain("must be a string");
  });

  it("refuses arguments to a command that declared none", () => {
    const before = mocks.state.toggleTheme.mock.calls.length;
    const outcome = executeCommandById("view.toggleTheme", builtinCommands(), { dark: "yes" });
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("takes no parameters");
    // Refused before the dispatch, not after: an ignored argument is a caller
    // that thinks it asked for something it did not get.
    expect(mocks.state.toggleTheme.mock.calls.length).toBe(before);
  });
});

/**
 * The three parameterised commands, driven through the *real* registry — the
 * same dispatch the relay uses, with only the store stubbed. What is under test
 * here is the half the relay deliberately does not do: whether the argument is
 * a member of the closed set the window owns.
 */
describe("the closed argument spaces", () => {
  it("refuses a layout name this window does not have, and names the ones it does", () => {
    const outcome = executeCommandById("layout.switch", builtinCommands(), {
      name: "Not a layout",
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain('no layout named "Not a layout"');
    // The whole point of a closed set is that the caller can be told what it is.
    expect(outcome.detail).toContain("Default");
  });

  it("refuses a workspace path that is not on the recent list", () => {
    const outcome = executeCommandById("workspace.open", builtinCommands(), {
      path: "C:\\somewhere\\else",
    });
    expect(outcome.ok).toBe(false);
    expect(outcome.detail).toContain("not on the recent workspaces list");
    // …and says how a person makes it legal, rather than only that it is not.
    expect(outcome.detail).toContain("Open it once from the workspace chip");
  });

  it("refuses a session cwd that is absolute or leaves the workspace", () => {
    const absolute = executeCommandById("session.start", builtinCommands(), {
      prompt: "hello",
      cwd: "C:\\Windows",
    });
    expect(absolute.ok).toBe(false);
    expect(absolute.detail).toContain("must be relative to the workspace");

    const escaping = executeCommandById("session.start", builtinCommands(), {
      prompt: "hello",
      cwd: "../..",
    });
    expect(escaping.ok).toBe(false);
    expect(escaping.detail).toContain("leaves the workspace");

    expect(mocks.state.createSessionIn).not.toHaveBeenCalled();
  });

  it("starts a session in the jailed folder and sends the prompt to it", async () => {
    const outcome = executeCommandById("session.start", builtinCommands(), {
      prompt: "summarise the dispatch model",
      cwd: "src/models/",
    });
    expect(outcome.ok).toBe(true);
    // The create/send pair is async; drain the microtasks the run() scheduled.
    await Promise.resolve();
    await Promise.resolve();
    expect(mocks.state.createSessionIn).toHaveBeenCalledWith("src/models");
    // Focused *before* the send, so a script that starts two sessions does not
    // put the second prompt into the first.
    expect(mocks.state.focusSession).toHaveBeenCalledWith("session-1");
    expect(mocks.state.sendChat).toHaveBeenCalledWith("summarise the dispatch model");
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
