/**
 * The Setup walkthrough's body, rendered.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design): what
 * is under test is *what the walkthrough says* and, load-bearingly, what it does
 * **not** offer — Claude login is a copyable instruction, never a button that
 * could log a user in. The three readiness states and the teach are asserted as
 * a pure function of a `SetupStatus`.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { Command } from "../commands";
import type { SetupCheck, SetupStatus } from "../types";

// The body pulls `dismissSetup` / `useSetup` from the descriptor module, whose
// `./dock` import reaches the whole registry (and Monaco). Nothing here calls
// them — the body is rendered with an explicit `onDismiss` prop.
vi.mock("../setup", () => ({ dismissSetup: () => undefined, useSetup: () => null }));
vi.mock("../api", () => ({ getSetupStatus: () => Promise.resolve(null) }));
// `allCommands` reaches the store (and the whole app graph) at import; the body
// takes its commands as a prop, so the real one is never needed here.
vi.mock("../commands", () => ({ allCommands: () => [] }));

import { SetupBody } from "./Setup";

function check(over: Partial<SetupCheck>): SetupCheck {
  return {
    id: "workspace",
    title: "Workspace",
    state: "ok",
    detail: "Working in project.",
    action: null,
    ...over,
  };
}

const CLAUDE_OUT: SetupCheck = check({
  id: "claude_login",
  title: "Claude",
  state: "action_needed",
  detail: "Claude is not signed in — run claude /login in a terminal.",
  action: {
    kind: "instruction",
    label: "Sign in from a terminal",
    command_id: null,
    instruction: "claude /login",
  },
});

const OFFICE_UNAVAIL: SetupCheck = check({
  id: "office",
  title: "Office",
  state: "unavailable",
  detail: "No Microsoft Office was found on this machine.",
});

const ONLYOFFICE_OK: SetupCheck = check({
  id: "onlyoffice",
  title: "OnlyOffice",
  state: "ok",
  detail: "OnlyOffice is configured for document preview and diff.",
});

function status(checks: SetupCheck[], over: Partial<SetupStatus> = {}): SetupStatus {
  return { checks, first_run: true, all_ok: false, ...over };
}

const TEACH_COMMANDS: Command[] = [
  { id: "keys.welcome", title: "Show the welcome card", run: () => undefined },
  { id: "keys.open", title: "Keyboard shortcuts", run: () => undefined },
];

function show(s: SetupStatus | null, commands: Command[] = TEACH_COMMANDS): string {
  return renderToStaticMarkup(
    <SetupBody status={s} commands={commands} onDismiss={() => undefined} />,
  );
}

describe("SetupBody", () => {
  it("renders each readiness state honestly", () => {
    const html = show(status([CLAUDE_OUT, OFFICE_UNAVAIL, ONLYOFFICE_OK]));
    // The three states, each with its word (colour is never the only signal).
    expect(html).toContain("Action needed");
    expect(html).toContain("Not available");
    expect(html).toContain("Ready");
    // The server's honest detail lines are shown verbatim.
    expect(html).toContain("No Microsoft Office was found on this machine.");
    expect(html).toContain("OnlyOffice is configured for document preview and diff.");
    // Each check's title.
    expect(html).toContain("Claude");
    expect(html).toContain("Office");
  });

  it("shows Claude login as a copyable instruction, never a button", () => {
    const html = show(status([CLAUDE_OUT]));
    // The exact command, in copyable text.
    expect(html).toContain("claude /login");
    expect(html).toContain('class="wb-setup-instruction"');
    // And crucially: no button carries the login. The only buttons on the page
    // are the teach affordances and "Got it" — never one labelled to sign in.
    expect(html).not.toContain("<button type=\"button\" class=\"wb-btn wb-btn-sm\">Sign in");
  });

  it("teaches the window by pointing at the welcome and reference, not duplicating them", () => {
    const html = show(status([ONLYOFFICE_OK]));
    expect(html).toContain("Learn the window");
    expect(html).toContain("Show the welcome card");
    expect(html).toContain("Keyboard shortcuts");
  });

  it("drops a teach affordance whose command is not registered", () => {
    const html = show(status([ONLYOFFICE_OK]), [TEACH_COMMANDS[0]]);
    expect(html).toContain("Show the welcome card");
    expect(html).not.toContain("Keyboard shortcuts");
  });

  it("renders an in-app command action as a button", () => {
    const withCommand: SetupCheck = check({
      id: "workspace",
      title: "Workspace",
      state: "action_needed",
      detail: "Pick a folder.",
      action: {
        kind: "command",
        label: "Switch workspace",
        command_id: "keys.open",
        instruction: null,
      },
    });
    const html = show(status([withCommand]));
    expect(html).toContain("Switch workspace");
  });

  it("says it is checking rather than showing an empty checklist before the read", () => {
    const html = show(null);
    expect(html).toContain("Checking what is connected");
    expect(html).not.toContain("wb-setup-row");
  });
});
