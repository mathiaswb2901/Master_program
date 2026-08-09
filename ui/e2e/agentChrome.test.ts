/**
 * ANVIL V5 — the agent + chat surface, as a contract
 * (DESIGN.md §2.4, §2.6, §6.3, §6.4, §6.12, §7).
 *
 * A vitest test rather than a Playwright one, beside `statusChrome.test.ts` and
 * `emptyState.test.ts` and for the same reason: the questions here are about
 * what a stylesheet *declares* — whether the five agent states are one table or
 * five opinions, whether a marker that is true all afternoon is wearing the one
 * colour reserved for motion, whether a decided permission card stops shouting
 * — and those are answered off disk in milliseconds. The live half is
 * `chat.spec.ts`, `plan.spec.ts` and `conversations.spec.ts`, which read what
 * the browser actually computed.
 *
 * The through-line: **this surface is where the app spends its colour, and
 * spending it on something that is still true when you look away is how the one
 * amber stops meaning anything** (§2.4). Every assertion below is a place a
 * plausible edit would start spending it — one of them on a marker that had
 * already shipped.
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

const AGENT = read("styles", "agent.css");
const PLAN = read("styles", "plan.css");
const CONVERSATIONS = read("styles", "conversations.css");
const SHEETS = [
  ["agent.css", AGENT],
  ["plan.css", PLAN],
  ["conversations.css", CONVERSATIONS],
] as const;

/** The two components that render this surface's class names. */
const CHAT_TSX = read("panels", "Chat.tsx");
const AGENT_TSX = read("panels", "AgentPanel.tsx");

/** DESIGN.md §2.6's five states, in the order the table lists them. */
const TONES = ["working", "attention", "idle", "done", "error"] as const;

describe("the agent-status vocabulary is one table (§2.6)", () => {
  it("paints every state, each from its own token", () => {
    for (const tone of TONES) {
      expect(body(AGENT, `.wb-dot.is-${tone}`)).toContain(`background: var(--agent-${tone})`);
    }
  });

  it("gives the hollow ring to a transcript, not a sixth colour (§6.12)", () => {
    // "On disk" is the absence of a state, so it is drawn as a shape.
    const disk = body(AGENT, ".wb-dot-disk");
    expect(disk).toContain("background: transparent");
    expect(disk).toContain("border: 1px solid var(--border-strong)");
  });

  it("pulses on exactly one state, and never in the stylesheet (§5.4)", () => {
    // The loop is `.u-agent-pulse` in tokens.css — one keyframe, stopped under
    // reduced motion in one place. A second `animation:` here would escape that.
    const offenders = rules(AGENT)
      .filter((rule) => /animation\s*:/.test(rule.body))
      .map((rule) => rule.selector);
    expect(offenders).toEqual([]);
    expect(CHAT_TSX).toContain("u-agent-pulse");
  });

  it("cross-fades a state change on the tint channel (§5.4)", () => {
    // These change because a session did, not because the user did; cutting
    // between colours is what makes a fleet read as numbers being overwritten.
    const dots = body(
      AGENT,
      TONES.map((tone) => `.wb-dot.is-${tone}`).join(", "),
    );
    expect(dots).toContain("transition: background-color var(--motion-tint)");
  });

  /**
   * The half every assertion above assumes and none of them checks: `is-<tone>`
   * is a *string join across two files*. The stylesheet names a state and
   * `Chat.tsx` emits it; get the string wrong and the rule matches nothing,
   * silently, in exactly the way an undefined `var()` paints nothing. So the
   * join is asserted from the other end — against the source that renders it.
   */
  it("names states the components really emit", () => {
    // The one place a tone becomes a class name.
    expect(CHAT_TSX).toContain("`wb-dot is-${v.tone}`");
    for (const tone of TONES) expect(CHAT_TSX).toContain(`tone: "${tone}"`);
    // And the picker speaks it rather than writing its own colours.
    expect(AGENT_TSX).toContain("dotClass(v)");
    expect(AGENT_TSX).not.toMatch(/style=\{\{\s*background:/);
  });

  it("keys the badge on the same names, and keeps its label readable (§7)", () => {
    for (const tone of TONES) {
      expect(body(AGENT, `.wb-badge.is-${tone}`)).toContain(`background: var(--agent-${tone}-bg)`);
    }
    // Measured departure from §6.4's "text = status color": at 11px the status
    // hue on its own wash is 4.05:1 (working) / 3.76:1 (attention) in light,
    // under §7's floor. Same finding, same answer, as V4 made in the bar.
    const labels = body(
      AGENT,
      TONES.map((tone) => `.wb-badge.is-${tone}`).join(", "),
    );
    expect(labels).toContain("color: var(--text-primary)");
  });
});

describe("no standing amber (§2.4)", () => {
  /**
   * The rule this file exists for. `--accent` marks *where I am* and *what is
   * changing right now*; a mark that is still true when you look away is the
   * failure §2.4's demotion table is a list of, and a permanent amber marker was
   * rejected once already in review.
   */
  const accentRules = (css: string): string[] =>
    rules(css)
      .filter((rule) => /var\(--accent|var\(--agent-working/.test(rule.body))
      .map((rule) => rule.selector);

  it("spends none of it in the conversation browser", () => {
    // `switch & open` was amber, and it is true for as long as the folder is
    // outside the workspace — on a hundred rows at once, in a browser. It is an
    // outlined neutral now, like the plan card's *Recommended*.
    expect(accentRules(CONVERSATIONS)).toEqual([]);
    const pill = body(CONVERSATIONS, ".wb-conv-switch");
    expect(pill).toContain("border: 1px solid var(--border-strong)");
    expect(pill).toContain("color: var(--text-secondary)");
  });

  it("keeps *Recommended* an outlined neutral (§2.4's demotion, held)", () => {
    const pill = body(PLAN, ".wb-plan-rec");
    expect(pill).not.toContain("--accent");
    expect(pill).toContain("border: 1px solid var(--border-strong)");
  });

  it("spends it in the plan card only on where-I-am and the blocked action", () => {
    // The chosen option, the annotate mode's rim and its pressed button. Not a
    // hover — §2.4 demoted plan-file and tool-call hovers, and a hover is
    // answered by the wash.
    expect(accentRules(PLAN).sort()).toEqual(
      [
        ".wb-plan-card.is-annotating",
        ".wb-plan-head .wb-btn.is-active",
        ".wb-plan-note-row.is-editing",
        ".wb-plan-option.is-selected",
        ".wb-plan-phrase.is-noted",
      ].sort(),
    );
  });

  it("spends it on the agent surface only on what is changing right now", () => {
    // The working dot, the working badge, and the tool row that is still
    // running — all three retract on their own. Nothing else, and in
    // particular not the chat box: the focus ring already says where the
    // keyboard is, and a second amber ring around the same element is the
    // QuickBar's demoted focus underline by another route (§2.4).
    expect(accentRules(AGENT).sort()).toEqual(
      [".wb-badge.is-working", ".wb-dot.is-working", ".wb-tool-row .wb-dot", ".wb-tool-row"].sort(),
    );
  });
});

describe("the chat's hierarchy (§6.3)", () => {
  it("keeps the column at 760px with the chat body's type", () => {
    const column = body(AGENT, ".wb-chat-col");
    expect(column).toContain("max-width: var(--chat-max-width)");
    expect(body(AGENT, ".wb-chat-list")).toContain("padding: var(--space-6)");
  });

  it("bubbles the user and leaves the assistant unwrapped", () => {
    const user = body(AGENT, ".wb-msg-user");
    expect(user).toContain("align-self: flex-end");
    expect(user).toContain("max-width: 85%");
    expect(user).toContain("background: var(--surface-elevated)");
    expect(user).toContain("border-radius: var(--radius-lg)");
    // The assistant's block has no surface of its own — documents read better
    // than chat toys. If one appears, this is where it shows up.
    expect(rules(AGENT).filter((rule) => rule.selector === ".wb-msg-block")).toEqual([]);
  });

  it("subordinates a tool call to the prose it belongs to", () => {
    // Inset from the column, so a run of tool calls reads as steps inside a
    // turn rather than as messages of their own.
    expect(body(AGENT, ".wb-tool")).toContain("padding-left: var(--space-3)");
    const row = body(AGENT, ".wb-tool-row");
    expect(row).toContain("min-height: var(--control-height)");
    expect(row).toContain("border-left: 2px solid var(--agent-working)");
    expect(row).toContain("font-family: var(--font-mono)");
    // Settling is a colour change and nothing else: a row that moved when it
    // finished would shove every row below it (§5.4).
    expect(row).toContain("transition: border-color var(--motion-tint-slow)");
    expect(body(AGENT, ".wb-tool-row.is-settled")).toContain("border-left-color: var(--success)");
    expect(body(AGENT, ".wb-tool-row.is-failed")).toContain("border-left-color: var(--error)");
  });

  it("puts the pulse on the tool row's dot and keeps the edge steady (§6.3)", () => {
    expect(body(AGENT, ".wb-tool-row .wb-dot")).toContain("background: var(--agent-working)");
    expect(body(AGENT, ".wb-tool-row.is-settled .wb-dot")).toContain("background: var(--success)");
    expect(body(AGENT, ".wb-tool-row.is-failed .wb-dot")).toContain("background: var(--error)");
    // …and the dot carries the name a 2px edge cannot (§7).
    expect(CHAT_TSX).toContain('aria-label={status}');
  });

  it("gives an interruption more air than a line of transcript", () => {
    expect(body(AGENT, ".wb-chat-col > .wb-perm-card, .wb-chat-col > .wb-plan-card")).toContain(
      "margin: var(--space-3) 0",
    );
  });

  it("rims a permission prompt in warn while it is a question (§6.3)", () => {
    const card = body(AGENT, ".wb-perm-card");
    expect(card).toContain("background: var(--surface-elevated)");
    expect(card).toContain("border: 1px solid var(--warn)");
    expect(body(AGENT, ".wb-perm-head")).toContain("background: var(--warn-bg)");
  });

  it("and drops the alarm once it has been answered (§2.5 — warn means now)", () => {
    // A card that keeps its rim after you decide is a standing warning about
    // something that already happened: the §2.4 mistake, one hue over.
    expect(body(AGENT, ".wb-perm-card.is-decided")).toContain("border-color: var(--border-default)");
    expect(body(AGENT, ".wb-perm-card.is-decided .wb-perm-head")).toContain(
      "background: transparent",
    );
    // The decision is said in a word and doubled by a dot, never colour alone.
    expect(body(AGENT, ".wb-perm-decision .wb-dot.is-allow")).toContain("background: var(--success)");
    expect(CHAT_TSX).toContain('item.decision === "allow" ? "Allowed" : "Denied"');
  });
});

describe("the session picker's reserved box (§6.12, §1.9)", () => {
  it("is reserved, capped at 40% of the pane, and never grows with its content", () => {
    expect(body(AGENT, ".wb-sessions")).toContain("max-height: 40%");
    const list = body(AGENT, ".wb-sessions-list");
    expect(list).toContain("flex: 0 1 var(--sessions-list-height)");
    expect(list).toContain("overflow-y: auto");
    // The fifth session is what summons the scrollbar; a reserved gutter is
    // what stops its arrival shifting every row inside a box built so that
    // nothing moves.
    expect(list).toContain("scrollbar-gutter: stable");
  });

  it("pins *New session* to the edge whatever appears beside the label (§1.9)", () => {
    // The live count is absent until the listing arrives. A `space-between`
    // header would therefore put the button next to the label on a cold launch
    // and slide it to the edge the moment the first session landed — found by
    // running the app with an empty workspace, not by reading the sheet.
    expect(body(AGENT, ".wb-sessions-header .wb-btn")).toContain("margin-left: auto");
    expect(body(AGENT, ".wb-sessions-header")).not.toContain("justify-content");
  });

  it("centres its empty state in the box that is there either way (§6.10)", () => {
    expect(body(AGENT, ".wb-sessions-none")).toContain("margin: auto");
    expect(AGENT_TSX).toContain("wb-sessions-none-hint");
  });

  it("keeps the folder label above its own rows while they scroll", () => {
    const folder = body(AGENT, ".wb-sessions-folder");
    expect(folder).toContain("position: sticky");
    // Opaque, because rows scroll under it.
    expect(folder).toContain("background: var(--surface-panel)");
  });

  it("says the ceiling on screen, not in a tooltip (§6.5's disabled-row rule)", () => {
    expect(AGENT_TSX).toContain("WORKBENCH_MAX_CONCURRENT_SESSIONS");
    expect(body(AGENT, ".wb-sessions-cap")).toContain("color: var(--text-tertiary)");
    // The reason used to be a `title` on the disabled button — the same silence
    // as hiding the row. It must not go back.
    expect(AGENT_TSX).not.toContain("title={full ?");
  });

  it("says what a session is doing in words as well as in hue (§7)", () => {
    expect(body(AGENT, ".wb-session-state")).toContain("text-transform: uppercase");
    expect(AGENT_TSX).toContain('<span className="wb-session-state">{v.label}</span>');
  });
});

describe("motion and colour discipline across the three sheets", () => {
  it("answers the pointer on the frame it arrives (§5.1.5)", () => {
    // Every hover that changes a colour on a surface whose base rule fades it.
    for (const selector of [
      ".wb-session-row:hover",
      ".wb-tool-chevron:hover",
      ".wb-tool-link:hover",
    ]) {
      expect(body(AGENT, selector)).toContain("transition-duration: 0s");
    }
    expect(body(PLAN, ".wb-plan-option:hover")).toContain("transition-duration: 0s");
    expect(body(PLAN, ".wb-plan-file:hover")).toContain("transition-duration: 0s");
    expect(body(CONVERSATIONS, ".wb-conv-row:hover")).toContain("transition-duration: 0s");
  });

  it("uses no deprecated duration alias — the channels are named (§5.8)", () => {
    for (const [file, css] of SHEETS) {
      const offenders = rules(css)
        .filter((rule) => /--duration-[123]|--ease-standard/.test(rule.body))
        .map((rule) => `${file}  ${rule.selector}`);
      expect(offenders).toEqual([]);
    }
  });

  it("floats nothing, so it casts no shadow (§1.4)", () => {
    // Structure is hairlines and surface steps here; the one ring in the set is
    // the annotate mode's, and it is an `outline`, which costs no layout.
    for (const [file, css] of SHEETS) {
      const offenders = rules(css)
        .filter((rule) => /box-shadow\s*:/.test(rule.body))
        .map((rule) => `${file}  ${rule.selector}`);
      expect(offenders).toEqual([]);
    }
    expect(body(PLAN, ".wb-plan-card.is-annotating")).toContain(
      "outline: 1px solid var(--accent-muted)",
    );
  });

  it("paints nothing with a raw hex (house rule, §2)", () => {
    for (const [file, css] of SHEETS) {
      const offenders = rules(css)
        .filter((rule) => /#[0-9a-fA-F]{3,8}\b/.test(rule.body))
        .map((rule) => `${file}  ${rule.selector}`);
      expect(offenders).toEqual([]);
    }
  });

  it("has rules to check, or every assertion above is vacuous", () => {
    expect(rules(AGENT).length).toBeGreaterThanOrEqual(40);
    expect(rules(PLAN).length).toBeGreaterThanOrEqual(30);
    expect(rules(CONVERSATIONS).length).toBeGreaterThanOrEqual(15);
  });
});
