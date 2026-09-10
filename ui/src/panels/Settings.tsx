/**
 * The Settings panel's body — the controls, and the one thing that is not one.
 *
 * Reached **only through a dynamic `import()`** from `ui/src/settings.ts`
 * (`React.lazy` + a warm on idle), so everything here — the controls and their
 * stylesheet — rides its own chunk and none of it is on the launch path
 * (`ui/e2e/perf/bundle.spec.ts`). The eager half (the store, the command, the
 * theme wiring) is the descriptor module.
 *
 * Three things it is careful about:
 *
 *  - **A control that cannot decide is not offered as one.** When the server
 *    reports an override — `WORKBENCH_OFFICE_NATIVE` set for this launch — the
 *    segments go disabled and the reason is printed underneath. A settings
 *    panel whose switch silently loses to an environment variable is the dead
 *    button the pane rules forbid.
 *  - **A setting that only applies at the next start says so**, from the
 *    server's own `pending_restart` rather than from a guess on this side.
 *  - **Telemetry is a statement, not a switch.** Zero telemetry is a product
 *    position; the server reports it as a fact with no shape in which it is on,
 *    and this renders that sentence. There is deliberately nothing to click.
 *
 * The whole surface is a pure function of the state, which is what makes each
 * of those states renderable in a unit test with no server (`Settings.test.tsx`).
 */

import type { IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import { refresh, save, useSettings } from "../settings";
import type { SettingKey, SettingsState, WorkbenchSettings } from "../types";

import "../styles/settings.css";

/** One choosable value of a setting. */
interface Choice<T extends string> {
  value: T;
  label: string;
}

const THEME_CHOICES: readonly Choice<WorkbenchSettings["theme"]>[] = [
  { value: "system", label: "System" },
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
];

const OFFICE_CHOICES: readonly Choice<WorkbenchSettings["office_native"]>[] = [
  { value: "auto", label: "Auto" },
  { value: "on", label: "On" },
  { value: "off", label: "Off" },
];

/** Voice is a boolean on the wire; the control is the same segmented pair as
 * everything else, so the panel reads as one vocabulary rather than three. */
const VOICE_CHOICES: readonly Choice<"off" | "on">[] = [
  { value: "off", label: "Off" },
  { value: "on", label: "On" },
];

/**
 * Retention is a number of days on the wire and a **choice** here, on purpose:
 * a free number field invites 1 (which throws away this morning's proof) and
 * 100000 (which is "forever" spelled unclearly), and neither is an answer anyone
 * arrived at. Four windows a person can mean — a month, a quarter, a year,
 * forever — expressed in the same segmented vocabulary as every other row, so
 * this PR adds a setting and not a control type. `0` is forever, which is the
 * server's own encoding rather than a second one invented for the UI.
 */
const RETENTION_CHOICES: readonly Choice<string>[] = [
  { value: "30", label: "30 days" },
  { value: "90", label: "90 days" },
  { value: "365", label: "1 year" },
  { value: "0", label: "Forever" },
];

/** The nearest offered window to whatever is stored — a document written by
 * hand (or by a later version with more choices) still lights a segment rather
 * than lighting none, which would read as "no value" for a setting that has one. */
export function retentionChoice(days: number): string {
  const offered = RETENTION_CHOICES.map((choice) => Number(choice.value));
  if (days <= 0 || offered.includes(days)) return String(Math.max(days, 0));
  const finite = offered.filter((value) => value > 0);
  const nearest = finite.reduce((best, value) =>
    Math.abs(value - days) < Math.abs(best - days) ? value : best,
  );
  return String(nearest);
}

/**
 * A segmented choice — a radio group wearing tabs' clothes.
 *
 * The selected segment is **neutral**, not amber: what it marks is a standing
 * property of a setting ("this is the value"), true for as long as the panel is
 * open, and DESIGN.md §2.4 spends the amber on *where I am* and *what is
 * changing now* only. The focus ring is the amber here, because that genuinely
 * is where you are.
 */
function Segmented<T extends string>({
  label,
  choices,
  value,
  disabled,
  onChange,
}: {
  label: string;
  choices: readonly Choice<T>[];
  value: T;
  disabled: boolean;
  onChange: (value: T) => void;
}) {
  return (
    <div className="wb-settings-segmented" role="radiogroup" aria-label={label}>
      {choices.map((choice) => (
        <button
          key={choice.value}
          type="button"
          role="radio"
          aria-checked={choice.value === value}
          disabled={disabled}
          className={`wb-settings-segment${choice.value === value ? " is-selected" : ""}`}
          onClick={() => {
            if (choice.value !== value) onChange(choice.value);
          }}
        >
          {choice.label}
        </button>
      ))}
    </div>
  );
}

/** One setting: what it is, what it does, its control, and any honest caveat. */
function Row({
  title,
  detail,
  note,
  children,
}: {
  title: string;
  detail: string;
  note?: string | null;
  children: React.ReactNode;
}) {
  return (
    <div className="wb-settings-row">
      <div className="wb-settings-row-text">
        <div className="wb-settings-row-title">{title}</div>
        <p className="wb-settings-row-detail">{detail}</p>
        {note != null && note !== "" ? <p className="wb-settings-note">{note}</p> : null}
      </div>
      <div className="wb-settings-row-control">{children}</div>
    </div>
  );
}

/** The note a row carries under its control: an override wins over a pending
 * restart, because a setting that is not in force at all has nothing to wait
 * for. `null` is the common case — most rows say nothing. */
export function noteFor(state: SettingsState, key: SettingKey): string | null {
  const override = state.overrides.find((entry) => entry.key === key);
  if (override !== undefined) return `${override.detail} In force: ${override.value}.`;
  if (state.pending_restart.includes(key)) return "Applies when Workbench restarts.";
  return null;
}

/**
 * The whole surface, as a pure function of the state. `null` is "not read yet"
 * — a plain loading line, never a form of defaults that would read as "these
 * are your settings" before anyone has asked the server.
 */
export function SettingsBody({
  state,
  error,
  onChange,
}: {
  state: SettingsState | null;
  error: string | null;
  onChange: (patch: Partial<WorkbenchSettings>) => void;
}) {
  return (
    <section className="wb-settings" aria-label="Settings">
      <header className="wb-settings-header">
        <h2 className="wb-settings-title">Settings</h2>
        <p className="wb-settings-lede">
          How this window behaves, remembered on this machine. These were environment
          variables; they are yours now.
        </p>
      </header>

      {error !== null ? (
        <p className="wb-settings-error" role="alert">
          {error}
        </p>
      ) : null}

      {state === null ? (
        <p className="wb-settings-loading u-label">Reading your settings…</p>
      ) : (
        <>
          {state.problem !== null ? (
            <p className="wb-settings-error" role="alert">
              {state.problem} — the defaults are in force.
            </p>
          ) : null}

          <div className="wb-settings-rows">
            <Row
              title="Theme"
              detail="System follows your OS setting. The window applies a change immediately."
              note={noteFor(state, "theme")}
            >
              <Segmented
                label="Theme"
                choices={THEME_CHOICES}
                value={state.stored.theme}
                disabled={state.overrides.some((entry) => entry.key === "theme")}
                onChange={(theme) => {
                  onChange({ theme });
                }}
              />
            </Row>

            <Row
              title="Open documents in Word and Excel"
              detail="Dock the real application into a pane instead of the browser preview. Auto uses it wherever this machine can; it needs the desktop app."
              note={noteFor(state, "office_native")}
            >
              <Segmented
                label="Open documents in Word and Excel"
                choices={OFFICE_CHOICES}
                value={state.stored.office_native}
                disabled={state.overrides.some((entry) => entry.key === "office_native")}
                onChange={(office_native) => {
                  onChange({ office_native });
                }}
              />
            </Row>

            <Row
              title="Voice input"
              detail="Push-to-talk dictation into the agent you are talking to, transcribed on this machine."
              note={
                noteFor(state, "voice_input") ??
                "The local transcriber is not installed yet — your answer is remembered for when it is."
              }
            >
              <Segmented
                label="Voice input"
                choices={VOICE_CHOICES}
                value={state.stored.voice_input ? "on" : "off"}
                disabled={state.overrides.some((entry) => entry.key === "voice_input")}
                onChange={(choice) => {
                  onChange({ voice_input: choice === "on" });
                }}
              />
            </Row>

            <Row
              title="Keep validation evidence"
              detail="Results, their evidence and their approvals are written into each project's own .workbench/validation/ folder, so a restart does not forget what was proven."
              // The honest half, said in the panel rather than left to be
              // discovered: this document is machine-local and the files it
              // governs are not, so one answer covers every project you open.
              // Not a `noteFor` lookup — there is no environment variable behind
              // this knob, so there is nothing an override could say about it.
              note="A choice about this machine's disk; the evidence itself stays in each project. A month is swept only once every result in it is past the window."
            >
              <Segmented
                label="Keep validation evidence"
                choices={RETENTION_CHOICES}
                value={retentionChoice(state.stored.validation_retention_days)}
                disabled={false}
                onChange={(days) => {
                  onChange({ validation_retention_days: Number(days) });
                }}
              />
            </Row>
          </div>

          <section className="wb-settings-privacy" aria-label="Privacy">
            <div className="wb-settings-privacy-head">
              <span className="wb-settings-row-title">Telemetry</span>
              {/* A statement, not a control: there is no shape of the server's
                  model in which this is on, so there is nothing to offer. */}
              <span className="wb-settings-stance">
                {state.telemetry.enabled ? "On" : "Off"}
              </span>
            </div>
            <p className="wb-settings-row-detail">{state.telemetry.detail}</p>
          </section>

          <footer className="wb-settings-footer">
            <span className="wb-settings-note">Stored on this machine</span>
            <code className="wb-settings-path" title={state.path}>
              {state.path}
            </code>
          </footer>
        </>
      )}
    </section>
  );
}

/** The dockview panel: wires this tool's store into the pure body, and re-reads
 * when it mounts — an override can have appeared since the last look. */
function SettingsPanelBody(_props: IDockviewPanelProps) {
  const state = useSettings((s) => s.state);
  const error = useSettings((s) => s.error);
  useEffect(() => {
    void refresh();
  }, []);
  return (
    <SettingsBody
      state={state}
      error={error}
      onChange={(patch) => {
        void save(patch);
      }}
    />
  );
}

export default SettingsPanelBody;
