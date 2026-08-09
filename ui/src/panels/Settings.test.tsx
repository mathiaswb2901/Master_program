/**
 * The Settings body, rendered — and the two things it must never render.
 *
 * Static markup rather than a DOM stack (the suite is node-only by design):
 * what is under test is *what the panel offers*. Two of the assertions are the
 * point of the whole surface — a telemetry **switch** is never offered (the
 * stance is a statement), and a control the environment has taken over is
 * disabled with the reason rather than left looking live.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { SettingsState } from "../types";

// The body pulls `refresh` / `save` / `useSettings` from the descriptor module,
// whose `./dock` import reaches the whole registry (and Monaco). Nothing here
// calls them: the body takes its state and its `onChange` as props.
vi.mock("../settings", () => ({
  refresh: () => Promise.resolve(),
  save: () => Promise.resolve(),
  useSettings: () => null,
}));

import { noteFor, SettingsBody } from "./Settings";

function state(over: Partial<SettingsState> = {}): SettingsState {
  const stored = { theme: "system" as const, office_native: "auto" as const, voice_input: false };
  return {
    stored,
    effective: stored,
    overrides: [],
    pending_restart: [],
    path: "C:\\Users\\a\\AppData\\Local\\Workbench\\settings.json",
    telemetry: { enabled: false, detail: "Off, and there is nothing to turn on." },
    problem: null,
    ...over,
  };
}

const render = (over: Partial<SettingsState> = {}, error: string | null = null): string =>
  renderToStaticMarkup(
    <SettingsBody state={state(over)} error={error} onChange={() => undefined} />,
  );

describe("the Settings body", () => {
  it("offers the three settings as radio groups", () => {
    const html = render();
    expect(html).toContain('aria-label="Theme"');
    expect(html).toContain('aria-label="Open documents in Word and Excel"');
    expect(html).toContain('aria-label="Voice input"');
    expect(html).toContain('role="radiogroup"');
  });

  it("marks the stored value as the checked segment", () => {
    const html = render({
      stored: { theme: "light", office_native: "off", voice_input: true },
    });
    // The selected segment is neutral-marked (`is-selected`) and checked; colour
    // is never the only signal, and it is never the amber (DESIGN.md §2.4).
    expect(html).toContain('aria-checked="true" class="wb-settings-segment is-selected">Light');
    expect(html).toContain('aria-checked="true" class="wb-settings-segment is-selected">Off');
    expect(html).toContain('aria-checked="true" class="wb-settings-segment is-selected">On');
  });

  it("states the telemetry stance and offers no way to change it", () => {
    const html = render();
    expect(html).toContain("Telemetry");
    expect(html).toContain("Off, and there is nothing to turn on.");
    // The one assertion this file exists for: the stance is not a control. If a
    // fourth radio group ever appears here, it is the telemetry switch that must
    // not exist.
    expect(html.match(/role="radiogroup"/g)).toHaveLength(3);
  });

  it("disables an overridden control and prints the reason", () => {
    const html = render({
      overrides: [
        {
          key: "office_native",
          value: "off",
          detail: "Set by WORKBENCH_OFFICE_NATIVE for this launch.",
        },
      ],
      effective: { theme: "system", office_native: "off", voice_input: false },
    });
    expect(html).toContain("Set by WORKBENCH_OFFICE_NATIVE for this launch.");
    expect(html).toContain("In force: off.");
    expect(html).toContain("disabled=");
    // …and only that one: the theme control is untouched.
    expect(html.match(/disabled=""/g)).toHaveLength(3); // the three Office segments
  });

  it("says a launch-only change is waiting for a restart", () => {
    const html = render({ pending_restart: ["office_native"] });
    expect(html).toContain("Applies when Workbench restarts.");
  });

  it("says voice is not installed yet rather than pretending it works", () => {
    expect(render()).toContain("The local transcriber is not installed yet");
  });

  it("shows where the document lives, so it is not a mystery", () => {
    expect(render()).toContain("settings.json");
    expect(render()).toContain("Stored on this machine");
  });

  it("reads as loading before the first answer, not as a form of defaults", () => {
    const html = renderToStaticMarkup(
      <SettingsBody state={null} error={null} onChange={() => undefined} />,
    );
    expect(html).toContain("Reading your settings…");
    expect(html).not.toContain("role=\"radiogroup\"");
  });

  it("surfaces a write failure and an unreadable document", () => {
    expect(render({}, "could not write settings: access is denied")).toContain(
      "could not write settings",
    );
    expect(render({ problem: "settings.json: not valid JSON" })).toContain(
      "the defaults are in force",
    );
  });
});

describe("noteFor", () => {
  it("says nothing when there is nothing to say", () => {
    expect(noteFor(state(), "theme")).toBeNull();
  });

  it("prefers the override to the pending restart", () => {
    // A setting that is not in force at all has nothing to wait for, so the
    // override is the only honest note.
    const note = noteFor(
      state({
        overrides: [{ key: "office_native", value: "on", detail: "Set outside the app." }],
        pending_restart: ["office_native"],
      }),
      "office_native",
    );
    expect(note).toBe("Set outside the app. In force: on.");
  });
});
