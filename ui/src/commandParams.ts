/**
 * Parameters a registered command takes — the window's half of PR-E.
 *
 * Three commands take arguments (`layout.switch{name}`, `workspace.open{path}`,
 * `session.start{prompt,cwd}`), and each one's argument space is a **closed set
 * the window already owns**: a layout it published, a folder on the recent list,
 * a folder under the workspace root. That is the design rule and not a
 * coincidence — it is what makes it safe for a CLI or an agent to reach in.
 *
 * The declaration lives here rather than in `commands.ts` so that the file which
 * names no capability keeps naming none: `commands.ts` gains a field of this
 * type, and everything about *what a parameter is* — the shape, the wire mirror,
 * the validation, the refusal wording — is in this module.
 *
 * **Two layers validate, and they check different things** (see
 * `server/src/workbench_server/services/commands.py`). The relay checks the
 * *shape* against the schema this module publishes, before the event bus: an
 * unknown field or a missing argument never reaches a window. This module checks
 * the same shape again at `run()` — because the manifest was a snapshot taken at
 * connect and this is the moment the command actually runs — and the command
 * itself then checks what the argument *means*.
 *
 * **Strings only.** Every argument in the shipped set is a name, a path or a
 * prompt. One type keeps the published manifest small enough to ride inside
 * `run_command`'s result budget, and keeps a refusal message short enough to be
 * worth reading. Widening it is a one-place change with the budget re-measured.
 */

import type { CommandParamsSchema } from "./types";

/**
 * Ceiling on one value, in characters, when a field names no tighter one.
 * Mirrors `MAX_PARAM_CHARS` in `models/commands.py` — the relay enforces the
 * same number a moment earlier, and the two must not disagree about which
 * invocations are legal.
 */
export const MAX_PARAM_CHARS = 4_000;

/** One argument a command takes. */
export interface CommandParamField {
  name: string;
  /** Default true — an optional field is the exception and says so. */
  required?: boolean;
  /** Cap on the value's length; `MAX_PARAM_CHARS` when absent. */
  maxLength?: number;
  /** A few words on what a valid value looks like, for the refusal and for the
   * agent's discovery listing. Kept short: it is paid on every listing. */
  detail?: string;
}

/** What a parameterised command declares about its arguments. */
export interface CommandParams {
  fields: readonly CommandParamField[];
  /**
   * Publish this command to the CLI/agent relay **even though
   * `isBindableFromFile` denies it**, on the standing condition that the relay
   * may then only invoke it *with* these arguments.
   *
   * Not a loophole in that bar — the opposite. `workspace.open` with no
   * arguments opens a folder dialog onto the whole filesystem, and
   * `session.start` with no arguments starts an agent; both are refused to
   * untrusted callers for good reasons. The *parameterised* form is strictly
   * narrower: a path that is not on the recent list the user built themselves,
   * or a folder outside the workspace, is refused by the command. So the flag
   * says "the arguments are the narrowing", and `executeCommandById` enforces
   * exactly that by refusing the bare gesture from the relay.
   *
   * A `shortcuts.md` file is unaffected: it binds a *chord*, which carries no
   * arguments, so `isBindableFromFile` still refuses it outright.
   */
  relayRequiresParams?: boolean;
}

/** Validated arguments, as handed to `run()`. */
export type CommandParamValues = Record<string, string>;

const limitOf = (field: CommandParamField): number => field.maxLength ?? MAX_PARAM_CHARS;

const isRequired = (field: CommandParamField): boolean => field.required !== false;

/** The wire schema for this declaration — what `buildManifest` publishes and
 * what the relay validates an incoming `params` against. */
export function paramsSchema(params: CommandParams): CommandParamsSchema {
  return {
    params: params.fields.map((field) => ({
      name: field.name,
      type: "string",
      required: isRequired(field),
      max_length: field.maxLength ?? null,
      detail: field.detail ?? "",
    })),
  };
}

export type ParamsOutcome =
  | { ok: true; values: CommandParamValues }
  | { ok: false; detail: string };

/**
 * Check an incoming `params` object against a declaration.
 *
 * Every refusal names the field and what would be accepted, on the same
 * reasoning the relay's refusals do: a caller told only "invalid params" spends
 * a round trip discovering which one, and a round trip is what this whole seam
 * exists to avoid. The relay refuses the same cases a moment earlier — this is
 * the check at the moment of the run, not a duplicate of a check that can be
 * skipped.
 */
export function validateParams(
  params: CommandParams,
  raw: Record<string, unknown>,
): ParamsOutcome {
  const known = new Map(params.fields.map((field) => [field.name, field]));
  const accepted = params.fields.map((field) => field.name).join(", ") || "none";
  for (const name of Object.keys(raw)) {
    if (!known.has(name)) return { ok: false, detail: `no parameter “${name}” — takes: ${accepted}` };
  }
  const values: CommandParamValues = {};
  for (const field of params.fields) {
    const value = raw[field.name];
    if (value === undefined) {
      if (!isRequired(field)) continue;
      const hint = field.detail === undefined ? "" : ` (${field.detail})`;
      return { ok: false, detail: `needs “${field.name}”${hint} — takes: ${accepted}` };
    }
    if (typeof value !== "string") {
      return { ok: false, detail: `“${field.name}” must be a string, got ${typeof value}` };
    }
    if (value.length > limitOf(field)) {
      return {
        ok: false,
        detail: `“${field.name}” is ${String(value.length)} characters; the limit is ${String(limitOf(field))}`,
      };
    }
    values[field.name] = value;
  }
  return { ok: true, values };
}
