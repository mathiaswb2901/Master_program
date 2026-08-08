/**
 * ANVIL V4 — the status bar and the toast layer, as a contract
 * (DESIGN.md §6.7, §6.4, §2.4, §2.6, §5.4, §7).
 *
 * A vitest test rather than a Playwright one, next to `emptyState.test.ts` and
 * for the same reason: the questions here are about what a stylesheet
 * *declares* — which reading is allowed to carry colour, whether the working
 * mark is an outline rather than a fill, whether a toast's kind is doubled as a
 * shape — and those are answered off disk in milliseconds. The live half is
 * `ambient.spec.ts` (computed tokens in a real browser) and `status.spec.ts`
 * (the journey the styling must not have changed).
 *
 * The through-line is the one this surface is easiest to get wrong: **the bar
 * is an instrument's readout, so colour in it is spent, not decorated.** Every
 * assertion below is a place a plausible edit would start spending it.
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { rules } from "./perf/css";

const STYLE_ROOT = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "src", "styles");

function read(name: string): string {
  return fs.readFileSync(path.join(STYLE_ROOT, name), "utf-8");
}

/** One rule's declarations, whitespace as `rules()` reports it. */
function body(css: string, selector: string): string {
  const found = rules(css).find((rule) => rule.selector === selector);
  expect(found, `expected a rule for \`${selector}\``).toBeDefined();
  return String(found?.body);
}

const STATUSBAR = read("statusbar.css");
const OVERLAYS = read("overlays.css");

/** Every rule whose selector names part of the status bar. */
const statusRules = (): { selector: string; body: string }[] =>
  rules(STATUSBAR).filter((rule) => /\.wb-status/.test(rule.selector));

/** Every rule whose selector names part of the toast layer. */
const toastRules = (): { selector: string; body: string }[] =>
  rules(OVERLAYS).filter((rule) => /\.wb-toast/.test(rule.selector));

describe("the bar itself (§6.7)", () => {
  const bar = () => body(STATUSBAR, ".wb-statusbar");

  it("is the 24px chrome strip on the app surface, hairlined off the dock", () => {
    expect(bar()).toContain("height: 24px");
    expect(bar()).toContain("background: var(--surface-app)");
    expect(bar()).toContain("border-top: 1px solid var(--border-subtle)");
    expect(bar()).toContain("font-size: var(--type-xs-size)");
  });

  it("renders every reading in tabular figures (§1.7)", () => {
    // A count that changes width as it counts is a count you have to re-read.
    expect(bar()).toContain("font-variant-numeric: tabular-nums");
  });

  it("is chrome, not content — a drag across it selects nothing", () => {
    expect(bar()).toContain("user-select: none");
  });

  it("gives an empty region no width and no gap — the quiet bar (§6.7)", () => {
    // "Counts hide at zero" is the Agent tool's job; this is the same doctrine
    // one level up, so a region that emptied does not leave its gap behind.
    expect(body(STATUSBAR, ".wb-statusbar > :empty")).toContain("display: none");
  });
});

describe("chips: one anatomy, and a focus ring that survives the clip (§6.4, §7)", () => {
  const chip = () => body(STATUSBAR, ".wb-status-chip");

  it("is an 18px pill with a hairline slot that is transparent at rest", () => {
    expect(chip()).toContain("height: 18px");
    expect(chip()).toContain("border-radius: var(--radius-full)");
    // Transparent rather than absent: gaining an edge must not move its
    // neighbours by 2px.
    expect(chip()).toContain("border: 1px solid transparent");
  });

  it("cross-fades its state on the tint channel, never on a spring (§5.4)", () => {
    const declarations = chip();
    expect(declarations).toContain("background-color var(--motion-tint)");
    expect(declarations).toContain("border-color var(--motion-tint)");
    expect(declarations).toContain("color var(--motion-tint)");
  });

  it("answers the pointer on the frame it arrives (§5.1.5)", () => {
    expect(body(STATUSBAR, ".wb-status-chip:hover")).toContain("transition-duration: 0s");
  });

  it("rings at 1px offset, because the bar clips its overflow", () => {
    const focus = body(STATUSBAR, ".wb-status-chip:focus-visible");
    expect(focus).toContain("outline: 2px solid var(--focus-ring)");
    // The global rule's 2px offset is sliced off an 18px chip in a 24px bar,
    // which is the one focus failure §7 cares about most.
    expect(focus).toContain("outline-offset: 1px");
    expect(focus).not.toContain("outline-offset: 2px");
  });
});

describe("the working chip — the bar's one 'changing right now' (§2.4)", () => {
  const working = () => body(STATUSBAR, '.wb-status-chip:has([aria-label="Working"])');

  it("wears the live amber as an outline, not as a fill", () => {
    // An outline composes with the active chip's `--surface-selected`; a fill
    // would compete with it, and two amber areas in one bar is how the one
    // colour that means motion stops meaning anything.
    expect(working()).toContain("border-color: var(--agent-working)");
    expect(working()).not.toContain("background");
  });

  it("is keyed to the accessible name the dot must carry anyway (§6.4)", () => {
    // The chips are the Agent tool's markup. The `aria-label` is the part of it
    // that is a contract — `status.spec.ts` locates the same name — so it is
    // what this sheet reads rather than a class it would have to ask for.
    expect(rules(STATUSBAR).map((rule) => rule.selector)).toContain(
      '.wb-status-chip:has([aria-label="Working"])',
    );
  });
});

describe("the attention badge — the one reading that is a request (§2.6, §7)", () => {
  const attention = () => body(STATUSBAR, '.wb-status-count:has([aria-label="Needs attention"])');

  it("is orchid, never amber: a standing state does not take the live colour", () => {
    expect(attention()).toContain("background: var(--agent-attention-bg)");
    expect(attention()).not.toContain("--agent-working");
    expect(attention()).not.toContain("--accent");
  });

  it("puts the colour on the dot and keeps the label readable (§7)", () => {
    // Measured, and it is why §6.4's "text = status colour" is not applied at
    // 11px here: `--agent-attention` on its own 16% wash over `--surface-app`
    // is 3.66:1 (dark) / 3.05:1 (light), under the 4.5:1 floor. `--text-primary`
    // on the same washes is 7.53:1 / 11.28:1.
    expect(attention()).toContain("color: var(--text-primary)");
  });

  it("leaves the working count unwashed — exactly one pill ever lights up", () => {
    const washed = statusRules().filter(
      (rule) => /\.wb-status-count/.test(rule.selector) && /background:/.test(rule.body),
    );
    expect(washed.map((rule) => rule.selector)).toEqual([
      '.wb-status-count:has([aria-label="Needs attention"])',
    ]);
  });
});

describe("colour is spent only where §6.7 and §6.9 sanction it", () => {
  it("spends no accent in this sheet — the layout chip owns it (§6.9)", () => {
    // `--surface-selected` (the active chip, §6.7) is a wash and is named
    // there; `--accent`, `--accent-muted` and `--accent-fill` belong to the
    // layout chip's own stylesheet and to nothing else in the bar.
    const offenders = statusRules()
      .filter((rule) => /var\(--accent/.test(rule.body))
      .map((rule) => rule.selector);
    expect(offenders).toEqual([]);
  });

  it("paints nothing with a raw hex (house rule, §2)", () => {
    const offenders = [...statusRules(), ...toastRules()]
      .filter((rule) => /#[0-9a-fA-F]{3,8}\b/.test(rule.body))
      .map((rule) => rule.selector);
    expect(offenders).toEqual([]);
  });

  it("has rules to check, or every assertion above is vacuous", () => {
    expect(statusRules().length).toBeGreaterThanOrEqual(10);
    expect(toastRules().length).toBeGreaterThanOrEqual(10);
  });
});

describe("toasts: the app's ambient voice (§2.1, §5.4, §7)", () => {
  it("floats on the one floating surface, with the border a shadow needs (§4)", () => {
    const toast = body(OVERLAYS, ".wb-toast");
    expect(toast).toContain("background: var(--surface-overlay)");
    expect(toast).toContain("border: 1px solid var(--border-default)");
    expect(toast).toContain("box-shadow: var(--shadow-2)");
  });

  it("arrives on the shared entrance and leaves shorter, without a spring (§5.4)", () => {
    expect(body(OVERLAYS, ".wb-toast")).toContain("animation: wb-rise-in var(--motion-enter)");
    const leaving = body(OVERLAYS, ".wb-toast.is-leaving");
    expect(leaving).toContain("animation: wb-slide-out var(--motion-exit)");
    // A dismissal that springs back is a dismissal arguing with the user.
    expect(leaving).not.toContain("celebrate");
  });

  it("spends the app's only bounce on success and on nothing else (§5.4)", () => {
    const bouncing = toastRules()
      .filter((rule) => /--motion-celebrate/.test(rule.body))
      .map((rule) => rule.selector);
    expect(bouncing).toEqual([".wb-toast.is-success"]);
  });

  it("hugs its message rather than reserving a 360px slab", () => {
    expect(body(OVERLAYS, ".wb-toasts")).toContain("align-items: flex-end");
    expect(body(OVERLAYS, ".wb-toast")).toContain("max-width: 100%");
  });

  it("never eats a click in the gaps between toasts", () => {
    expect(body(OVERLAYS, ".wb-toasts")).toContain("pointer-events: none");
    expect(body(OVERLAYS, ".wb-toast")).toContain("pointer-events: auto");
  });

  it("says its kind with a shape as well as a colour (§7)", () => {
    // Colour is never the only signal: each kind tints the 2px edge *and* the
    // 14px mark, and `Toasts.tsx` names the kind for a screen reader.
    for (const [kind, token] of [
      ["error", "--error"],
      ["warn", "--warn"],
      ["info", "--info"],
      ["success", "--success"],
    ] as const) {
      expect(body(OVERLAYS, `.wb-toast.is-${kind}`)).toContain(`border-left-color: var(${token})`);
      expect(body(OVERLAYS, `.wb-toast.is-${kind} .wb-toast-mark`)).toContain(
        `color: var(${token})`,
      );
    }
  });

  it("gives its one control a 24px target (§7, WCAG 2.2)", () => {
    const close = body(OVERLAYS, ".wb-toast-close");
    expect(close).toContain("width: 24px");
    expect(close).toContain("height: 24px");
  });
});
