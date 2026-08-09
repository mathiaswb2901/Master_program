/**
 * The voice control, as a contract (DESIGN.md §2.4, §5.4, §5.6, §7).
 *
 * A vitest test beside `agentChrome.test.ts`, `statusChrome.test.ts` and
 * `emptyState.test.ts`, and for the same reason: the questions here are about
 * what a stylesheet *declares*, and those are answered off disk in
 * milliseconds. The live half — a real press, a real transcript arriving, the
 * pane-independence round trip — is `ui/e2e/voice.spec.ts`.
 *
 * The through-line is one rule. **A recording indicator is the one thing in
 * this app that is genuinely allowed to pulse in amber, and it is allowed
 * because it stops.** §2.4's demotion table is a list of marks that were still
 * true when you looked away; an open microphone never is — it is true for
 * exactly as long as a finger is on the button. Every assertion below is a
 * place where a plausible edit would turn that transient mark into a standing
 * one, or would spend the amber somewhere that has nothing to do with the
 * microphone being open.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules } from "./perf/css";

const SRC = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src");

const read = (...parts: string[]): string => fs.readFileSync(path.join(SRC, ...parts), "utf-8");

/** One rule's declarations, whitespace as `rules()` reports it. */
function body(css: string, selector: string): string {
  const found = rules(css).find((rule) => rule.selector === selector);
  expect(found, `expected a rule for \`${selector}\``).toBeDefined();
  return String(found?.body);
}

const VOICE = read("styles", "voice.css");
const VOICE_TSX = read("voice.tsx");
const CHAT_TSX = read("panels", "Chat.tsx");
const AGENT_TSX = read("panels", "AgentPanel.tsx");

describe("the recording mark is transient, not standing (§2.4)", () => {
  it("spends the amber only on the control that is actually recording", () => {
    const accent = rules(VOICE)
      .filter((rule) => /var\(--accent/.test(rule.body))
      .map((rule) => rule.selector)
      .sort();
    // The recording button and its live dot. Nothing else — in particular not
    // the idle button, which is true all afternoon on every open composer.
    expect(accent).toEqual([".wb-voice-btn.is-recording", ".wb-voice-live"]);
  });

  it("leaves the idle control achromatic", () => {
    const idle = body(VOICE, ".wb-voice-btn");
    expect(idle).not.toContain("--accent");
    expect(idle).not.toContain("color:");
    // It inherits `.wb-btn-outline`'s neutral chrome rather than restating it.
    expect(VOICE_TSX).toContain("wb-btn wb-btn-outline wb-voice-btn");
  });

  it("only renders the live mark while the microphone is open", () => {
    // The class-name join across the two files: the stylesheet paints
    // `.wb-voice-live` and the component must emit it, and must emit it under a
    // condition that ends. A mark rendered unconditionally is the §2.4 failure.
    expect(VOICE_TSX).toContain('className="wb-voice-live u-agent-pulse"');
    expect(VOICE_TSX).toContain("{recording && <span");
    expect(VOICE_TSX).toContain('(recording ? " is-recording" : "")');
  });
});

describe("motion (§5.4, §5.6)", () => {
  it("declares no animation of its own — the pulse is the app's one keyframe", () => {
    // `.u-agent-pulse` is defined once in `tokens.css` and stopped once under
    // `prefers-reduced-motion`. A second `@keyframes` or `animation:` here would
    // escape that single point of control.
    expect(VOICE).not.toContain("@keyframes");
    const animated = rules(VOICE)
      .filter((rule) => /animation\s*:/.test(rule.body))
      .map((rule) => rule.selector);
    expect(animated).toEqual([]);
    expect(VOICE_TSX).toContain("u-agent-pulse");
  });

  it("restates no transition, so the button keeps `.wb-btn`'s hover feel", () => {
    // Instant in, eased out is decided once in `app.css` (§5.1.5). A second
    // opinion here is how one control ends up feeling different from its row.
    const transitions = rules(VOICE)
      .filter((rule) => /transition/.test(rule.body))
      .map((rule) => rule.selector);
    expect(transitions).toEqual([]);
  });

  it("uses no deprecated duration alias (§5.8)", () => {
    const offenders = rules(VOICE)
      .filter((rule) => /--duration-[123]|--ease-standard/.test(rule.body))
      .map((rule) => rule.selector);
    expect(offenders).toEqual([]);
  });
});

describe("colour is never the only signal (§7)", () => {
  it("says the state in words beside the mark", () => {
    expect(VOICE_TSX).toContain('{recording ? "Listening" : "Dictate"}');
    expect(body(VOICE, ".wb-voice-note")).toContain("color: var(--text-tertiary)");
    // …and announces it, once per state change rather than per interim word.
    expect(VOICE_TSX).toContain('role="status"');
  });

  it("carries a pressed state and a name that teaches both gestures", () => {
    expect(VOICE_TSX).toContain("aria-pressed={recording}");
    expect(VOICE_TSX).toContain("hold to talk, or press to toggle");
  });

  it("says on screen that the audio never leaves the machine", () => {
    // The privacy posture is a user-facing string, not only a design note. No
    // surface in this feature may imply a cloud transcriber.
    expect(VOICE_TSX).toContain("Audio is transcribed on this machine and never leaves it");
    expect(VOICE_TSX).toContain("Listening on this machine");
    // And when the words are canned, it says so rather than looking like it works.
    expect(VOICE_TSX).toContain("scripted: no microphone is open");
    for (const source of [VOICE_TSX, CHAT_TSX]) {
      expect(source).not.toMatch(/cloud|Google|upload/i);
    }
  });
});

describe("how it joins the app", () => {
  it("paints nothing with a raw hex (house rule, §2)", () => {
    const offenders = rules(VOICE)
      .filter((rule) => /#[0-9a-fA-F]{3,8}\b/.test(rule.body))
      .map((rule) => rule.selector);
    expect(offenders).toEqual([]);
  });

  it("is a control in the composer, not a panel", () => {
    // Voice fills the session you are talking to; it registers no tool and takes
    // no line in `tools.ts`. Its command and chord belong to the Agent tool,
    // which is the capability that owns "which conversation am I talking to".
    expect(CHAT_TSX).toContain("<VoiceButton");
    expect(AGENT_TSX).toContain('id: "voice.dictate"');
    expect(AGENT_TSX).toContain('"voice.dictate": ["Alt+V"]');
    const tools = read("tools.ts");
    expect(tools).not.toContain("voice");
  });

  it("adds no sixth entry to the agent-status table (§2.6)", () => {
    // Recording is not an agent state. Reusing `.wb-dot.is-*` for it would blur
    // what those five colours mean, which is the one thing that table is for.
    const dots = rules(VOICE)
      .map((rule) => rule.selector)
      .filter((selector) => selector.includes(".wb-dot"));
    expect(dots).toEqual([]);
    expect(VOICE_TSX).not.toContain("wb-dot");
  });

  it("has rules to check, or every assertion above is vacuous", () => {
    expect(rules(VOICE).length).toBeGreaterThanOrEqual(5);
  });
});
