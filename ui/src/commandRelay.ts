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
 * static built-in commands that pass `isRelayInvocable` — `isBindableFromFile`'s
 * bar (item 4's, for untrusted input, which an external caller is a case of),
 * plus the one narrowing addition PR-E adds. A command that re-points the path
 * jail is still refused *as a gesture*; `workspace.open{path}` is admitted only
 * because the parameters are what narrow it, and this module enforces that by
 * refusing an argument-less invoke of one (`executeCommandById`). An id absent
 * from the published set is refused even if it reached us.
 *
 * **`params` reaches `run()` here, and until PR-E it did not.** The relay has
 * always carried a `params` object; `executeCommandById` called `command.run()`
 * with no arguments, so it was dropped on the floor at this end while
 * `buildManifest` hardcoded `takes_params: false` at the other. Both ends are
 * now real: the manifest publishes the schema a command declares, and a
 * validated object is handed to `run`.
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
import { paramsSchema, validateParams } from "./commandParams";
import type { Command } from "./commands";
import { isRelayInvocable } from "./registry";
import type { WorkbenchTool } from "./registry";
import type {
  CommandInvokeEvent,
  CommandManifest,
  CommandManifestItem,
  WorkspaceEvent,
} from "./types";
import { ReconnectingSocket } from "./ws";

/**
 * The commands this window will run on request: the ones that pass the
 * untrusted-input bar. Dynamic commands (one per saved layout, per recent
 * workspace) are excluded upstream — the caller passes `builtinCommands()`, the
 * static set — and the families that reach a filesystem path are exactly the
 * ones `isBindableFromFile` blocks and `isRelayInvocable` re-admits only in
 * their parameterised form.
 */
export function invocableCommands(commands: readonly Command[]): Command[] {
  return commands.filter((command) => isRelayInvocable(command));
}

/**
 * The manifest published on connect.
 *
 * A command that declares `params` publishes its schema and says
 * `takes_params: true`; every other one publishes neither, and that asymmetry is
 * a budget rather than a style. The whole manifest is what an agent gets back
 * from a `run_command` discovery call, which is capped at
 * `RUN_COMMAND.max_result_bytes` (2,560, ~2,250 already spent on `id :: title`
 * rows) — a schema on every row would not fit, and three compact hints do.
 */
export function buildManifest(commands: readonly Command[]): CommandManifest {
  return {
    commands: invocableCommands(commands).map((command): CommandManifestItem => {
      const declared = command.params;
      return {
        id: command.id,
        title: command.title,
        takes_params: declared !== undefined,
        ...(declared === undefined ? {} : { params_schema: paramsSchema(declared) }),
      };
    }),
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
 * is exactly what was published. Re-checks the safety bar, the command's live
 * `when()` and its arguments here too: the manifest was a snapshot at connect,
 * and this is the moment the command actually runs.
 *
 * The refusal for a command whose *only* admission is its parameters — the
 * `relayRequiresParams` case — is where that flag stops being a claim and starts
 * being enforced. An empty `params` on one of those is the bare gesture wearing
 * a schema, and it is refused here, not narrowed later.
 *
 * **That refusal reads the flag itself**, not `isBindableFromFile`. The two
 * happen to select the same two commands today — both of PR-E's
 * `relayRequiresParams` declarations are also denied by the from-file bar — but
 * they are logically independent, and testing the wrong one is a refusal that
 * evaporates the moment a command is both safe enough to be bindable from a
 * file *and* declares that the relay may only invoke it with arguments. Such a
 * command would fall straight through to `validateParams`, which passes an empty
 * object whenever every field is optional, and `run({})` would be the bare
 * gesture the flag exists to forbid. Reading `declared.relayRequiresParams` is
 * strictly wider than the old check — anything not bindable from file has
 * already been refused above by `isRelayInvocable` unless it set the flag — so
 * nothing that was refused becomes runnable.
 */
export function executeCommandById(
  id: string,
  commands: readonly Command[],
  params: Record<string, unknown> = {},
): CommandOutcome {
  const command = commands.find((candidate) => candidate.id === id);
  if (command === undefined) return { ok: false, detail: `no command “${id}”` };
  if (!isRelayInvocable(command)) {
    return { ok: false, detail: `“${id}” cannot be invoked from outside the window` };
  }
  if (command.when?.() === false) {
    return { ok: false, detail: `“${id}” is not available right now` };
  }
  const declared = command.params;
  if (declared === undefined) {
    const extra = Object.keys(params);
    if (extra.length > 0) {
      return { ok: false, detail: `“${id}” takes no parameters (got ${extra.sort().join(", ")})` };
    }
  }
  let values: Record<string, string> | undefined;
  if (declared !== undefined) {
    if (Object.keys(params).length === 0 && declared.relayRequiresParams === true) {
      const names = declared.fields.map((field) => field.name).join(", ");
      return { ok: false, detail: `“${id}” may only be invoked from outside with: ${names}` };
    }
    const outcome = validateParams(declared, params);
    if (!outcome.ok) return { ok: false, detail: `“${id}”: ${outcome.detail}` };
    values = outcome.values;
  }
  try {
    command.run(values);
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
  const outcome = executeCommandById(event.command_id, await builtins(), event.params);
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

/** Stop the relay and, if it was running, unpublish the manifest first. The
 * backend keeps the last-published manifest until it is replaced, so a graceful
 * teardown (workspace switch, window close) that just dropped the socket would
 * leave a stale manifest: `GET /api/commands` would keep listing this window's
 * commands as available and every invoke would block the full invoke timeout
 * before returning "did not confirm" instead of the immediate honest "no window
 * is connected". Publishing an empty manifest on the way out restores that
 * honest state. Fire-and-forget — teardown does not wait on the network, and a
 * hard crash that never runs this is still bounded by the server's invoke
 * timeout. */
export function stopCommandRelay(): void {
  if (socket !== null) void publishCommandManifest({ commands: [] });
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
