/**
 * The first-run walkthrough's body — the *connect* checklist and a short teach
 * that composes with the welcome card (M7 §2).
 *
 * This module is reached **only through a dynamic `import()`** from
 * `ui/src/setup.tsx` (`React.lazy` + a warm on idle), so everything here — the
 * checklist UI and its stylesheet — rides its own chunk and none of it is on the
 * launch path (`ui/e2e/perf/bundle.spec.ts`). The eager half (the store, the
 * status chip, the auto-open lifecycle) lives in the descriptor module.
 *
 * What it shows, and what it deliberately does not:
 *
 *  - **the readiness checklist** — one row per connection, a §6.4 status pill +
 *    the server's honest detail line + at most one action. Claude login carries
 *    a **copyable instruction** (`claude /login`), never a button that logs in;
 *    an in-app `command` action runs a registered command;
 *  - **the teach** — it does *not* re-list the window basics the welcome card
 *    owns (panes, the QuickBar, the tools). It points at them, as real command
 *    affordances, and gets out of the way.
 *
 * The body is a pure function of a `SetupStatus`, so its three states are
 * rendered and asserted with no DOM stack (`Setup.test.tsx`).
 */

import type { IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import { getSetupStatus } from "../api";
import { allCommands, type Command } from "../commands";
import { dismissSetup, useSetup } from "../setup";
import type { SetupCheck, SetupStatus } from "../types";

import "../styles/setup.css";

/** The commands the teach points at — window basics the welcome card owns, named
 * here only to link to them, never to duplicate their prose. */
const TEACH_COMMAND_IDS = ["keys.welcome", "keys.open"] as const;

/** The pill's modifier class per state. Colour is never the only signal — the
 * label carries the word too (DESIGN.md §6.4). */
const PILL_CLASS: Record<SetupCheck["state"], string> = {
  ok: "is-ok",
  action_needed: "is-action",
  unavailable: "is-unavailable",
};

const PILL_LABEL: Record<SetupCheck["state"], string> = {
  ok: "Ready",
  action_needed: "Action needed",
  unavailable: "Not available",
};

function StatusPill({ state }: { state: SetupCheck["state"] }) {
  return (
    <span className={`wb-setup-pill ${PILL_CLASS[state]}`}>
      <span className="wb-setup-dot" aria-hidden="true" />
      {PILL_LABEL[state]}
    </span>
  );
}

/** One check's action, if any. An `instruction` is copyable text the human runs
 * themselves — never wired to a handler (the safety posture: the app cannot log
 * a user in). A `command` runs a registered window command. */
function CheckAction({ check, commands }: { check: SetupCheck; commands: readonly Command[] }) {
  const action = check.action;
  if (action === null) return null;
  if (action.kind === "instruction" && action.instruction !== null) {
    return (
      <div className="wb-setup-action">
        <span className="wb-setup-action-label u-label">{action.label}</span>
        <code className="wb-setup-instruction">{action.instruction}</code>
      </div>
    );
  }
  if (action.kind === "command" && action.command_id !== null) {
    const command = commands.find((c) => c.id === action.command_id);
    if (command === undefined) return null;
    return (
      <div className="wb-setup-action">
        <button
          type="button"
          className="wb-btn wb-btn-sm"
          onClick={() => command.run()}
        >
          {action.label}
        </button>
      </div>
    );
  }
  return null;
}

function CheckRow({ check, commands }: { check: SetupCheck; commands: readonly Command[] }) {
  return (
    <div className={`wb-setup-row is-${check.state}`}>
      <div className="wb-setup-row-head">
        <span className="wb-setup-row-title">{check.title}</span>
        <StatusPill state={check.state} />
      </div>
      <p className="wb-setup-detail">{check.detail}</p>
      <CheckAction check={check} commands={commands} />
    </div>
  );
}

/** The teach: real affordances into the window basics, resolved from the
 * registry so a row for a gated-off command simply does not appear. */
function Teach({ commands }: { commands: readonly Command[] }) {
  const actions = TEACH_COMMAND_IDS.flatMap((id) => {
    const command = commands.find((c) => c.id === id);
    return command === undefined ? [] : [command];
  });
  if (actions.length === 0) return null;
  return (
    <section className="wb-setup-teach" aria-label="Learn the window">
      <div className="wb-setup-teach-title u-label">Learn the window</div>
      <p className="wb-setup-teach-lede">
        Every pane splits, every tool goes in any pane, and everything here has a keyboard
        path. Two places to start:
      </p>
      <div className="wb-setup-teach-actions">
        {actions.map((command) => (
          <button
            key={command.id}
            type="button"
            className="wb-btn wb-btn-sm wb-btn-ghost"
            onClick={() => command.run()}
          >
            {command.title}
          </button>
        ))}
      </div>
    </section>
  );
}

/**
 * The whole surface, as a pure function of the status — which is what makes its
 * states renderable without a browser. `null` is "not read yet": a plain loading
 * line, never an empty checklist that reads as "nothing is connected".
 */
export function SetupBody({
  status,
  commands,
  onDismiss,
}: {
  status: SetupStatus | null;
  commands: readonly Command[];
  onDismiss: () => void;
}) {
  return (
    <section className="wb-setup" aria-label="Set up Workbench">
      <header className="wb-setup-header">
        <h2 className="wb-setup-title">Let's get you connected</h2>
        <p className="wb-setup-lede">
          What Workbench can reach on this machine, said plainly. This is the only time it
          asks — answer it once and it stays out of your way.
        </p>
      </header>

      {status === null ? (
        <p className="wb-setup-loading u-label">Checking what is connected…</p>
      ) : (
        <div className="wb-setup-checks">
          {status.checks.map((check) => (
            <CheckRow key={check.id} check={check} commands={commands} />
          ))}
        </div>
      )}

      <Teach commands={commands} />

      <footer className="wb-setup-footer">
        <span className="wb-setup-note">
          You can reopen this any time from the command palette — “Show setup”.
        </span>
        <button type="button" className="wb-btn wb-btn-sm" onClick={onDismiss}>
          Got it
        </button>
      </footer>
    </section>
  );
}

/** The dockview panel: wires the store and the registry into the pure body, and
 * refreshes the honest status when it mounts (the store may hold a read from
 * dock-ready that is a moment stale). */
function SetupPanelBody(_props: IDockviewPanelProps) {
  const status = useSetup((s) => s.status);
  const commands = allCommands();
  useEffect(() => {
    // Re-read on open: the chip's read can be seconds old, and "did I sign in
    // yet?" is exactly the question a user opens this to re-answer.
    void refreshOnOpen();
  }, []);
  return <SetupBody status={status} commands={commands} onDismiss={dismissSetup} />;
}

/** Ask the server again when the panel opens. The `api` module is a static
 * import from this (already-dynamic) chunk, so it adds no launch cost — the
 * whole file is off the entry path. */
async function refreshOnOpen(): Promise<void> {
  try {
    useSetup.setState({ status: await getSetupStatus() });
  } catch {
    // leave whatever the store holds; the body shows the last honest read
  }
}

export default SetupPanelBody;
