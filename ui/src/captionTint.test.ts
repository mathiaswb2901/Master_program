/**
 * The mapping from design tokens to the payload the shell paints the frame
 * with, and the two ways it is allowed to refuse.
 *
 * The other half of this module's contract — that the palette it names is
 * *legible* in both themes — is `e2e/captionContrast.test.ts`, which has to
 * read `tokens.css` off disk and therefore has to live outside `tsc -b`'s
 * program (the same reason, and the same technique, as
 * `e2e/perf/rowGeometry.test.ts`).
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { applyCaptionTint, CAPTION_TOKENS, captionTintFrom, opaqueHex } from "./captionTint";

describe("token → frame payload", () => {
  const palette: Record<string, string> = {
    "--surface-app": "#14161A",
    "--text-secondary": "rgb(168, 176, 188)",
    "--border-strong": "#3D4450",
  };
  const read = (token: string): string => palette[token] ?? "";

  it("normalises whatever the token resolved to", () => {
    // `hexVar` already funnels rgb()/#rgb through `toHex`; this asserts the
    // shape that leaves for the shell, which is the contract `caption.rs`
    // parses and refuses anything else.
    expect(captionTintFrom((token) => read(token))).toEqual({
      caption: "#14161a",
      text: "#a8b0bc",
      border: "#3d4450",
    });
  });

  it("maps each part of the frame to its documented token", () => {
    expect(CAPTION_TOKENS).toEqual({
      caption: "--surface-app",
      text: "--text-secondary",
      border: "--border-strong",
    });
  });

  it("sends nothing at all when a token does not resolve", () => {
    // All-or-nothing: two of our colours and one of Windows' reads as a
    // rendering bug, which is worse than the stock frame it replaced.
    for (const token of Object.values(CAPTION_TOKENS)) {
      const missing = { ...palette, [token]: "" };
      expect(captionTintFrom((name) => missing[name] ?? "")).toBeNull();
    }
  });

  it("drops an alpha channel rather than refusing the colour", () => {
    // A COLORREF has no alpha. A translucent chrome token is a decision this
    // API cannot honour, and ignoring the channel is nearer the intent than
    // painting nothing.
    expect(opaqueHex("#14161aE6")).toBe("#14161a");
    expect(opaqueHex("  #14161A  ")).toBe("#14161a");
  });

  it("refuses anything that is not a hex colour", () => {
    for (const bad of ["", "#abc", "14161a", "rgb(20, 22, 26)", "transparent", "#gggggg"]) {
      expect(opaqueHex(bad)).toBeNull();
    }
  });
});

describe("applying it", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  /** A document whose computed tokens are `palette`. */
  function stubTokens(palette: Record<string, string>): void {
    vi.stubGlobal("document", { documentElement: {} });
    vi.stubGlobal("getComputedStyle", () => ({
      getPropertyValue: (name: string) => palette[name] ?? "",
    }));
  }

  it("reads the tokens of the theme on screen and never throws at the shell", async () => {
    stubTokens({
      "--surface-app": "#ECEEF1",
      "--text-secondary": "#4B5563",
      "--border-strong": "#B9C0C9",
    });
    // Outside the shell the call is swallowed (`shell.test.ts` pins that);
    // what matters here is that the whole path runs and resolves, because
    // App.tsx fires it from an effect where a rejection would be unhandled.
    await expect(applyCaptionTint()).resolves.toBeUndefined();
  });

  it("says so and sends nothing when the palette is not there", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    stubTokens({});
    await expect(applyCaptionTint()).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledOnce();
  });
});
