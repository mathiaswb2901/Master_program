/**
 * The machine's Office sign-in, as one quiet line in the doc panel.
 *
 * Two halves, both driven by a mocked identity payload — no Office, no window:
 *
 *  1. `identityLine`, the pure reduction to the one thing the panel says. This
 *     is where "signed in as X", the honest "sign into Word to edit" degrade,
 *     and the never-fabricate rule for an unknown/unnameable account live.
 *  2. `OfficeIdentityLine`, the component, rendered for the three states the
 *     endpoint can report: signed-in-licensed, unsigned, and unknown. It reads
 *     the store, so the states are seeded there and the markup asserted.
 *
 * Importing the panel drags in the tool modules, so the two things that cannot
 * load outside a browser are stubbed exactly as `registry.test.ts` does them:
 * Monaco's bundle and the store (which reads `document` at import). Neither is
 * under test here.
 */

import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { identityLine } from "./officeHost";
import type { OfficeIdentity } from "./types";

vi.mock("./monaco", () => ({
  MONO_FONT: "mono",
  editorPathProp: (path: string) => path,
  languageForPath: () => "plaintext",
  monacoThemeName: () => "workbench",
  setActiveEditor: () => undefined,
  disposeModel: () => undefined,
  setModelContent: () => null,
  defineWorkbenchTheme: () => undefined,
  loadMonaco: () => Promise.resolve({}),
  prefetchMonaco: () => undefined,
}));

vi.mock("./store", () => ({
  useStore: Object.assign(() => undefined, { getState: () => ({ activePath: null }) }),
  emptyPlanDraft: () => ({ choices: {}, annotations: {}, comment: "", verdict: null }),
  unchosenOptionGroups: () => [],
}));

const { IdentityLineView } = await import("./panels/OfficeHostPanel");

const identity = (over: Partial<OfficeIdentity>): OfficeIdentity => ({
  signed_in: true,
  active: { display_name: "Analyst", email: "analyst@example.com" },
  accounts: [{ display_name: "Analyst", email: "analyst@example.com" }],
  license: "licensed",
  detail: "",
  ...over,
});

describe("identityLine — the one quiet line, reduced", () => {
  it("names the account when signed in and licensed", () => {
    expect(identityLine(identity({}))).toEqual({ text: "Signed in as Analyst", degraded: false });
  });

  it("falls back to the email when there is no friendly name", () => {
    const line = identityLine(identity({ active: { display_name: null, email: "a@b.com" } }));
    expect(line).toEqual({ text: "Signed in as a@b.com", degraded: false });
  });

  it("degrades to the honest 'sign into Word' when nobody is signed in", () => {
    const line = identityLine(identity({ signed_in: false, active: null, license: "unknown" }));
    expect(line).toEqual({ text: "Sign into Word to edit", degraded: true });
  });

  it("nudges gently, without alarm, when signed in but unlicensed", () => {
    const line = identityLine(identity({ license: "unlicensed" }));
    expect(line).toEqual({
      text: "Signed in as Analyst — sign in to Word to edit",
      degraded: true,
    });
  });

  it("still names the account, no claim of edit rights, when the license is unknown", () => {
    expect(identityLine(identity({ license: "unknown" }))).toEqual({
      text: "Signed in as Analyst",
      degraded: false,
    });
  });

  it("shows nothing rather than fabricate a name it does not have", () => {
    // Signed in, several accounts, registry cannot say which is active: omit.
    const line = identityLine(
      identity({ active: null, accounts: [], license: "unknown" }),
    );
    expect(line).toBeNull();
  });

  it("renders nothing before the identity has been fetched", () => {
    expect(identityLine(null)).toBeNull();
  });
});

describe("IdentityLineView — the three states, rendered", () => {
  const html = (payload: OfficeIdentity | null): string =>
    renderToStaticMarkup(<IdentityLineView line={identityLine(payload)} />);

  it("signed-in-licensed: shows 'Signed in as Analyst'", () => {
    const out = html(identity({}));
    expect(out).toContain("Signed in as Analyst");
    expect(out).toContain('role="status"');
    expect(out).toContain('data-degraded="false"');
  });

  it("unsigned: shows the honest degrade, marked degraded", () => {
    const out = html(identity({ signed_in: false, active: null, license: "unknown" }));
    expect(out).toContain("Sign into Word to edit");
    expect(out).toContain('data-degraded="true"');
  });

  it("unknown/unnameable: renders nothing alarming at all", () => {
    expect(html(identity({ active: null, accounts: [], license: "unknown" }))).toBe("");
  });
});
