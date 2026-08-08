/**
 * The command relay's window half: publish what this window will run, and run
 * what the backend relays (M5 item 14).
 *
 * The window owns the command registry, so a shell or an agent cannot execute a
 * command directly — the backend relays one over `/ws/events` and this module is
 * the executor at the far end. It does two things, and self-registers as a tool
 * (`tools.ts`) so neither is wired by editing `App.tsx`, `commands.ts` or
 * `StatusBar.tsx`:
 *
 *  1. on every (re)connect it PUBLISHES the manifest — the commands it is willing
 *     to run — so the backend can list them for discovery and validate an
 *     incoming id against them;
 *  2. on a `command_invoke` event it looks the id up in the SAME registry the
 *     QuickBar and keymap use and runs it through the SAME `run()` — no second
 *     execution path — then reports success or failure back.
 *
 * Safety is not this module inventing a policy: the manifest is exactly the
 * static built-in commands that pass `isBindableFromFile` — the bar item 4 set
 * for untrusted input, which an external caller is a case of — so a command that
 * re-points the path jail (`workspace.open`) is neither published nor runnable
 * here, and an id absent from that set is refused even if it reached us.
 *
 * The command list is reached through a dynamic `import("./commands")` at
 * runtime, never a static import: `commands.ts` imports the tool registry, so a
 * static edge from here would close a load-time cycle (`tools.ts` -> this ->
 * `commands.ts` -> `tools.ts`) that leaves this tool `undefined` in the array.
 * The list is only needed after connect and on an invoke — both long after the
 * module graph settles — so the pure helpers take the list as an argument and
 * the runtime fetches it lazily.
 *
 * It opens its own `/ws/events` socket rather than sharing the store's: the
 * store's dispatch names the events it cares about and drops the rest, and a
 * capability that self-registers should not have to edit that shared dispatch to
 * hear one more event type. The bus fans out to every subscriber, so the second
 * socket sees `command_invoke` and the store's socket ignores it — no double run.
 */

import { publishCommandManifest, reportCommandResult } from "./api";
import type { Command } from "./commands";
import { isBindableFromFile } from "./registry";
import type { WorkbenchTool } from "./registry";
import type { CommandInvokeEvent, CommandManifest, WorkspaceEvent } from "./types";
import { ReconnectingSocket } from "./ws";

/**
 * The commands this window will run on request: the ones that pass the
 * untrusted-input bar. Dynamic commands (one per saved layout, per recent
 * workspace) are excluded upstream — the caller passes `builtinCommands()`, the
 * static set — and the two families that reach a filesystem path are exactly the
 * ones `isBindableFromFile` blocks.
 */
export function invocableCommands(commands: readonly Command[]): { id: string; title: string }[] {
  return commands
    .filter((command) => isBindableFromFile(command))
    .map((command) => ({ id: command.id, title: command.title }));
}

/** The manifest published on connect. `takes_params` is false for every command
 * today — they run parameterless — and is here so a parameterised one can say so
 * later without a wire change. */
export function buildManifest(commands: readonly Command[]): CommandManifest {
  return {
    commands: invocableCommands(commands).map(({ id, title }) => ({
      id,
      title,
      takes_params: false,
    })),
  };
}

export interface CommandOutcome {
  ok: boolean;
  detail: string;
}

/**
 * Run one command by id through the registry's own dispatch, or say why not.
 *
 * Resolved against the same list the manifest is built from, so what is runnable
 * is exactly what was published. Re-checks the safety bar and the command's live
 * `when()` here too: the manifest was a snapshot at connect, and this is the
 * moment the command actually runs.
 */
export function executeCommandById(id: string, commands: readonly Command[]): CommandOutcome {
  const command = commands.find((candidate) => candidate.id === id);
  if (command === undefined) return { ok: false, detail: `no command “${id}”` };
  if (!isBindableFromFile(command)) {
    return { ok: false, detail: `“${id}” cannot be invoked from outside the window` };
  }
  if (command.when?.() === false) {
    return { ok: false, detail: `“${id}” is not available right now` };
  }
  try {
    command.run();
    return { ok: true, detail: command.title };
  } catch (err) {
    return { ok: false, detail: err instanceof Error ? err.message : String(err) };
  }
}

/** The static built-in command list, fetched lazily (see the module note). */
async function builtins(): Promise<readonly Command[]> {
  const { builtinCommands } = await import("./commands");
  return builtinCommands();
}

async function publish(): Promise<void> {
  await publishCommandManifest(buildManifest(await builtins()));
}

async function handleInvoke(event: CommandInvokeEvent): Promise<void> {
  const outcome = executeCommandById(event.command_id, await builtins());
  await reportCommandResult({
    invocation_id: event.invocation_id,
    ok: outcome.ok,
    detail: outcome.detail,
  });
}

let socket: ReconnectingSocket | null = null;

/** Open the relay socket, publish the manifest on connect, and execute on
 * `command_invoke`. Idempotent — a second call while running is a no-op. */
export function startCommandRelay(): void {
  if (socket !== null) return;
  socket = new ReconnectingSocket("/ws/events", {
    onOpen: () => {
      void publish();
    },
    onMessage: (data) => {
      const event = data as WorkspaceEvent;
      if (event.type === "command_invoke") void handleInvoke(event);
    },
  });
}

export function stopCommandRelay(): void {
  socket?.close();
  socket = null;
}

/**
 * Registers the relay as a tool with no panel and no command of its own — it
 * contributes a lifecycle, not a surface. `onDockReady` is the app-is-up signal:
 * it fires once with the dock and again with `null` on teardown, which is
 * exactly the start/stop the socket wants. The relay does not touch the dock —
 * commands do their own arranging through `run()` — so the api is ignored.
 */
export const commandRelayTool: WorkbenchTool = {
  id: "command-relay",
  title: "Command relay",
  onDockReady: (api) => {
    if (api !== null) startCommandRelay();
    else stopCommandRelay();
  },
};
