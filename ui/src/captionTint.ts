/**
 * The native window frame, painted from the app's own palette.
 *
 * The first pixel of Workbench is a caption Windows draws, and until this
 * module existed it drew it in the system's colours: a stock grey title bar
 * above a graphite app. Windows 11 lets an application *tint* that caption
 * rather than replace it (`DwmSetWindowAttribute`), so the frame becomes ours
 * while the window manager keeps drawing it — see `caption.rs` for the Win32
 * half and for what happens on a build that has no such attributes.
 *
 * **Why the colours are read here and not written in Rust.** They are design
 * tokens (`ui/src/design/tokens.css`), and that file is the single source of
 * truth for every colour in the product — DESIGN.md is explicit that components
 * consume tokens and never raw hex. A hex constant in the shell would be a
 * second palette, wrong the first time the first one is edited, and invisible
 * to the theme toggle. So the UI resolves the tokens of the theme currently on
 * screen and hands the shell three literal colours; the shell knows nothing
 * about themes.
 *
 * **Which token paints what** (DESIGN.md §2.1 — `--surface-app` *is* "window
 * chrome: title bar, dock tab strips, activity/status bars", so the caption
 * continues the strip the status bar and tab strips already use):
 *
 * | Part | Token | Why |
 * |---|---|---|
 * | caption background | `--surface-app` | the window-chrome surface, both themes |
 * | title text | `--text-secondary` | §1.1 chrome recedes; ≥ 4.5:1 on `--surface-app` in both themes, asserted in `captionTint.test.ts` |
 * | window border | `--border-strong` | §1.4 hairlines define structure; the heaviest border token, because this edge separates the app from an unknown desktop |
 */

import { setCaptionTint, type CaptionTint } from "./shell";
import { cssVar, toHex } from "./theme";

/** Token per part of the frame. Ordered as the table above. */
export const CAPTION_TOKENS: Readonly<Record<keyof CaptionTint, string>> = {
  caption: "--surface-app",
  text: "--text-secondary",
  border: "--border-strong",
};

/**
 * A token value as the opaque `#rrggbb` the DWM can take, or `null`.
 *
 * A caption colour has no alpha channel — `COLORREF`'s high byte is not one —
 * so an `#RRGGBBAA` token is accepted with its alpha dropped rather than
 * refused: a translucent chrome token is a decision this API cannot honour, and
 * ignoring the channel is closer to the intent than painting nothing. Anything
 * that is not a hex colour at all (an unresolved token reads back as `""`, and
 * `toHex` passes through what it cannot parse) returns `null`, because sending
 * the shell a string it will only reject is a round trip that buys nothing.
 */
export function opaqueHex(value: string): string | null {
  const match = /^#([0-9a-f]{6})(?:[0-9a-f]{2})?$/i.exec(value.trim());
  return match === null ? null : `#${match[1].toLowerCase()}`;
}

/**
 * Resolve the three tokens through `read`, or `null` if any of them is not a
 * colour.
 *
 * `read` returns a raw token value — whatever the stylesheet wrote, which may
 * be `#rgb` or an `rgb()` — and normalising it is this function's job, so the
 * seam a test injects at is the same one the DOM sits behind.
 *
 * All-or-nothing on purpose: a frame with two of our colours and one of
 * Windows' is worse than the stock frame it replaced, because it reads as a
 * rendering bug rather than as a default.
 */
export function captionTintFrom(read: (token: string) => string): CaptionTint | null {
  const caption = opaqueHex(toHex(read(CAPTION_TOKENS.caption)));
  const text = opaqueHex(toHex(read(CAPTION_TOKENS.text)));
  const border = opaqueHex(toHex(read(CAPTION_TOKENS.border)));
  if (caption === null || text === null || border === null) return null;
  return { caption, text, border };
}

/** The tint for the theme painted **right now** — computed tokens, not the store. */
export const captionTint = (): CaptionTint | null => captionTintFrom(cssVar);

/**
 * Send the current theme's frame colours to the shell.
 *
 * Called on startup and on every theme flip (`App.tsx`). A no-op in a browser
 * tab, where `shell.ts` swallows the call, and a no-op here if the palette does
 * not resolve — with a console line, since the shell cannot report a call it
 * never received.
 */
export async function applyCaptionTint(): Promise<void> {
  const tint = captionTint();
  if (tint === null) {
    console.warn("caption tint skipped: the frame tokens did not resolve to colours");
    return;
  }
  await setCaptionTint(tint);
}
