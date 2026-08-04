/**
 * Bridge from CSS design tokens to JS-configured surfaces (Monaco, xterm).
 * Colors are always read from the computed tokens of the current theme — the
 * single source of truth stays src/design/tokens.css; no hex lives in TS.
 */

import type { ITheme } from "@xterm/xterm";

export type Theme = "dark" | "light";

export const THEME_STORAGE_KEY = "workbench-theme";

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Normalize a token value (#RGB / #RRGGBB / #RRGGBBAA or rgb()/rgba()) to hex. */
export function toHex(cssColor: string): string {
  const c = cssColor.trim();
  if (c.startsWith("#")) {
    return c.length === 4 ? "#" + [...c.slice(1)].map((ch) => ch + ch).join("") : c;
  }
  const m = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)$/.exec(c);
  if (!m) return c;
  const to2 = (n: number): string =>
    Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, "0");
  const [, r, g, b, a] = m;
  let hex = `#${to2(Number(r))}${to2(Number(g))}${to2(Number(b))}`;
  if (a !== undefined && a !== "") hex += to2(Number(a) * 255);
  return hex;
}

/** Computed token value as hex, e.g. hexVar("--accent") -> "#5C9CE6". */
export const hexVar = (name: string): string => toHex(cssVar(name));

/** xterm.js theme built from the current tokens (DESIGN.md §2.7 / §6.6). */
export function xtermTheme(): ITheme {
  return {
    background: hexVar("--surface-terminal"),
    foreground: hexVar("--text-primary"),
    cursor: hexVar("--accent"),
    cursorAccent: hexVar("--surface-terminal"),
    selectionBackground: hexVar("--surface-selected"),
    black: hexVar("--ansi-black"),
    red: hexVar("--ansi-red"),
    green: hexVar("--ansi-green"),
    yellow: hexVar("--ansi-yellow"),
    blue: hexVar("--ansi-blue"),
    magenta: hexVar("--ansi-magenta"),
    cyan: hexVar("--ansi-cyan"),
    white: hexVar("--ansi-white"),
    brightBlack: hexVar("--ansi-bright-black"),
    brightRed: hexVar("--ansi-bright-red"),
    brightGreen: hexVar("--ansi-bright-green"),
    brightYellow: hexVar("--ansi-bright-yellow"),
    brightBlue: hexVar("--ansi-bright-blue"),
    brightMagenta: hexVar("--ansi-bright-magenta"),
    brightCyan: hexVar("--ansi-bright-cyan"),
    brightWhite: hexVar("--ansi-bright-white"),
  };
}
