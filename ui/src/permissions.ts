/**
 * The window's record of every permission prompt it has been shown, and the
 * three folds that move one.
 *
 * **Why a record rather than a field on the card.** A prompt reaches a human on
 * two channels: the session's own `/ws/agent` socket, which is what a chat pane
 * renders, and the fleet-wide `session_permission` frame, which is what Mission
 * Control renders — and which the server also publishes when the prompt is
 * answered *anywhere* or hits its ten-minute timeout
 * (`services/agent_sessions.ask_permission`). A card that remembered its own
 * click therefore kept asking a question that was already settled, and clicking
 * it again reached a prompt the server had closed (404, by design: see
 * `resolve_permission`). One record per `request_id` — request ids are unique
 * across sessions — read by every surface, is what makes the two channels one
 * truth instead of two that drift.
 *
 * Split out of `store.ts` for the reason `provenance.ts` and `liveTranscript.ts`
 * were: the store reaches `document` and `localStorage` at import and so cannot
 * be loaded in the node unit environment, while *what settles a prompt, and
 * what must not* is exactly the decision this file exists to get right. The
 * integrated path — two surfaces, one click — is proved in
 * `e2e/agent-surfaces.spec.ts`.
 */

/**
 * How a prompt ended.
 *
 * `settled` is the honest third answer. The fleet frame says only that a prompt
 * is *gone*, never which way it went, so a window that did not answer it knows
 * the question is closed and nothing more. Rendering "Allowed" there would be
 * inventing a decision the agent may never have received — the same lie the plan
 * card refuses to tell when it settles from `plan_resolved`.
 */
export type PermissionOutcome = "allow" | "deny" | "settled";

/** One prompt, as this window knows it. */
export interface PermissionRecord {
  requestId: string;
  /**
   * The session that is blocked.
   *
   * Carried on the record because it is what an answer is addressed to, and a
   * card lives in a pane that is not necessarily the focused one: reading the
   * session off `activeSessionId` at click time answers for whichever
   * conversation the keyboard happened to be in (CLAUDE.md, panes §1).
   */
  sessionId: string;
  tool: string;
  description: string;
  /** null while it is still a question. */
  decision: PermissionOutcome | null;
}

export type PermissionRecords = Readonly<Record<string, PermissionRecord>>;

/**
 * Note a prompt this window has been shown.
 *
 * Idempotent on purpose: the server replays still-pending prompts on every
 * (re)connect of `/ws/agent`, so the same request arrives more than once — and
 * a replay that reset an answered record to "asking" would put the buttons back
 * under a decision the user already made.
 */
export function notePermission(
  records: PermissionRecords,
  request: Omit<PermissionRecord, "decision">,
): Record<string, PermissionRecord> {
  const known = records[request.requestId];
  if (known !== undefined) return records as Record<string, PermissionRecord>;
  return { ...records, [request.requestId]: { ...request, decision: null } };
}

/**
 * Settle one prompt.
 *
 * Never overwrites a decision already recorded: the first answer is the one the
 * agent received, and the retraction frame that follows it carries no verdict of
 * its own (see `PermissionOutcome`). Returns the map unchanged — by reference,
 * so a subscriber does not re-render — when there is nothing to move.
 */
export function settlePermission(
  records: PermissionRecords,
  requestId: string,
  outcome: PermissionOutcome,
): Record<string, PermissionRecord> {
  const record = records[requestId];
  if (record === undefined || record.decision !== null) {
    return records as Record<string, PermissionRecord>;
  }
  return { ...records, [requestId]: { ...record, decision: outcome } };
}

/**
 * Settle every prompt of one session that the server no longer lists.
 *
 * The fleet frame carries the **whole** open set for one session, including the
 * empty set, so anything of that session's that is missing from it is closed:
 * answered here, answered in another window, answered from Mission Control, or
 * timed out. Prompts belonging to other sessions are untouched — one session's
 * frame says nothing about another's.
 */
export function settleVanished(
  records: PermissionRecords,
  sessionId: string,
  open: readonly string[],
): Record<string, PermissionRecord> {
  const stillOpen = new Set(open);
  let next: Record<string, PermissionRecord> | null = null;
  for (const record of Object.values(records)) {
    if (record.sessionId !== sessionId) continue;
    if (record.decision !== null || stillOpen.has(record.requestId)) continue;
    next ??= { ...records };
    next[record.requestId] = { ...record, decision: "settled" };
  }
  return next ?? (records as Record<string, PermissionRecord>);
}
