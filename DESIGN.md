# Workbench Design System

Binding spec for all UI in Workbench (Tauri + React, dockview layout, Monaco, xterm.js,
OnlyOffice, agent chat, QuickBar). Every color/size named here exists as a CSS custom
property in `ui/src/design/tokens.css`. Components consume tokens — never raw hex.

Style direction: **Swiss-modern precision workbench** — strict grid, hairline borders,
one accent, zero decoration, dark-first graphite chrome. Chosen because the app hosts
three "loud" embedded surfaces (Monaco, xterm, white Office documents); the chrome must
be a calm neutral frame that makes each content surface look deliberate, and because the
audience (energy/finance analysts) reads density and restraint as quality. Deliberately
rejected: OLED-black + neon (dev toy), glassmorphism (GPU cost in a many-panel window),
blue-navy bases (clash with white document paper).

---

## 1. Principles

1. **Chrome recedes, content owns the light.** Panel chrome is low-contrast graphite;
   the brightest things on screen are the user's document, code, and data — never our UI.
2. **Paper is a first-class surface.** A white OnlyOffice canvas inside a dark window is
   a feature, not a bug: document panels are framed as "paper" with a rim
   (`--border-paper-rim`) and a dedicated surround color, in both themes.
3. **One accent, spent sparingly.** `--accent` marks exactly: focus, selection, the
   primary action, and "agent working". If everything glows, nothing does.
4. **Hairlines, not shadows, define structure.** Panels separate with 1px borders and
   surface steps. Shadows exist only on things that float (menus, QuickBar, tooltips).
5. **Density is respect.** 13px base UI, 26px rows, 34px bars. Analysts want more on
   screen, not bigger buttons. Minimum hit target 24×24px (desktop, WCAG 2.2).
6. **Motion is confirmation, never entertainment.** 80–200ms, opacity/transform only.
   Anything tracking the pointer (sash drag, resize) moves 1:1 with zero animation.
7. **Numbers are data.** Anything numeric (prices, sizes, times, line numbers) renders
   in tabular figures (`font-variant-numeric: tabular-nums`) or the mono font.
8. **Keyboard-first.** Every interactive element has a visible 2px focus ring; every
   surface is reachable without a mouse; QuickBar (Ctrl+K) can reach anything.

---

## 2. Color system

Graphite ramp at hue ≈ 220°, saturation 8–12% — near-neutral, faintly cool. Accent is a
calm steel blue. Semantic hues are battle-tested dark/light pairs (≥ 4.5:1 against their
text surfaces, ≥ 3:1 as UI indicators).

### 2.1 Surfaces

| Token | Dark | Light | Use |
|---|---|---|---|
| `--surface-app` | `#14161A` | `#ECEEF1` | Window chrome: title bar, dock tab strips, activity/status bars |
| `--surface-panel` | `#1A1D22` | `#F7F8FA` | Panel bodies: file tree, chat, editor gutter background |
| `--surface-elevated` | `#21252C` | `#FFFFFF` | Cards, inputs, user chat bubble, hovered dropzones |
| `--surface-overlay` | `#262B33` | `#FFFFFF` | QuickBar, menus, popovers, tooltips (always + `--shadow-3`) |
| `--surface-terminal` | `#111317` | `#FFFFFF` | xterm.js background (deepest dark surface) |
| `--surface-paper` | `#FFFFFF` | `#FFFFFF` | Document canvas ("paper") — identical in both themes |
| `--surface-paper-surround` | `#262B33` | `#E4E7EB` | Area around the page inside a document panel |
| `--surface-hover` | `rgba(255,255,255,0.05)` | `rgba(17,19,23,0.05)` | Hover wash on rows/tabs/buttons |
| `--surface-active` | `rgba(255,255,255,0.08)` | `rgba(17,19,23,0.08)` | Pressed wash |
| `--surface-selected` | `rgba(92,156,230,0.14)` | `rgba(46,111,208,0.10)` | Selected row/tab/list item |
| `--backdrop` | `rgba(0,0,0,0.45)` | `rgba(15,18,23,0.30)` | Modal/QuickBar scrim |

### 2.2 Text

| Token | Dark | Light | Use |
|---|---|---|---|
| `--text-primary` | `#E6E9EE` | `#1B1F26` | Headings, active tab, primary content |
| `--text-secondary` | `#A8B0BC` | `#4B5563` | Body in panels, inactive-but-relevant |
| `--text-tertiary` | `#78808D` | `#6E7781` | Metadata, timestamps, inactive tabs, placeholders |
| `--text-disabled` | `#545B66` | `#9AA1AB` | Disabled controls only |
| `--text-on-accent` | `#FFFFFF` | `#FFFFFF` | Text on accent-filled buttons |
| `--text-on-paper` | `#1B1F26` | `#1B1F26` | Our chrome drawn over paper surfaces (both themes) |

### 2.3 Borders

| Token | Dark | Light | Use |
|---|---|---|---|
| `--border-subtle` | `#262A31` | `#E4E7EB` | Panel-to-panel hairlines, row separators |
| `--border-default` | `#2E333C` | `#D7DBE1` | Inputs, cards, buttons |
| `--border-strong` | `#3D4450` | `#B9C0C9` | Hover/active borders, dividers needing weight |
| `--border-paper-rim` | `#3D4450` | `#D7DBE1` | 1px rim framing document ("paper") panels |
| `--focus-ring` | `#5C9CE6` | `#2E6FD0` | 2px focus outline everywhere |

### 2.4 Accent

| Token | Dark | Light |
|---|---|---|
| `--accent` | `#5C9CE6` | `#2E6FD0` |
| `--accent-hover` | `#75ADEC` | `#275FB5` |
| `--accent-active` | `#4A8AD4` | `#21519B` |
| `--accent-muted` | `rgba(92,156,230,0.15)` | `rgba(46,111,208,0.12)` |

`--accent` on `--surface-panel` ≈ 6.0:1 (dark) and 4.9:1 on white (light) — usable as
link/text color, not just fills.

### 2.5 Semantic

| Token | Dark | Light | Tinted bg (dark / light) |
|---|---|---|---|
| `--success` | `#3FB950` | `#1A7F37` | `rgba(63,185,80,0.14)` / `rgba(26,127,55,0.10)` |
| `--warn` | `#D29922` | `#9A6700` | `rgba(210,153,34,0.14)` / `rgba(154,103,0,0.10)` |
| `--error` | `#F85149` | `#CF222E` | `rgba(248,81,73,0.14)` / `rgba(207,34,46,0.10)` |
| `--info` | `#58A6FF` | `#0969DA` | `rgba(88,166,255,0.14)` / `rgba(9,105,218,0.10)` |

### 2.6 Agent status

Dedicated tokens (do not reuse semantic tokens directly, even where values match — the
meaning is different and may diverge later).

| Token | Dark | Light | Meaning |
|---|---|---|---|
| `--agent-working` | `#5C9CE6` | `#2E6FD0` | Agent running — dot pulses (see §6) |
| `--agent-attention` | `#D29922` | `#9A6700` | Needs permission / user input — dot steady |
| `--agent-idle` | `#78808D` | `#6E7781` | Session open, nothing running |
| `--agent-done` | `#3FB950` | `#1A7F37` | Finished since last viewed |
| `--agent-error` | `#F85149` | `#CF222E` | Failed / crashed |

### 2.7 Terminal ANSI palette

Harmonized 16-color set (tokens `--ansi-*` in tokens.css). Dark: black `#2A2E37`, red
`#F47067`, green `#57AB5A`, yellow `#C69026`, blue `#6CB6FF`, magenta `#B083F0`, cyan
`#39C5CF`, white `#A8B0BC`; brights `#545B66 #FF938A #6BC46D #DAAA3F #96D0FF #DCBDFB
#56D4DD #E6E9EE`. Light: black `#1B1F26`, red `#CF222E`, green `#1A7F37`, yellow
`#9A6700`, blue `#0969DA`, magenta `#8250DF`, cyan `#1B7C83`, white `#6E7781`; brights
`#4B5563 #A40E26 #116329 #7D5400 #218BFF #A475F9 #3192AA #8C959F`. Feed these to
xterm.js `theme` and to the Monaco theme's token colors so terminal, editor, and chrome
read as one system.

### 2.8 Monaco / OnlyOffice harmonization

- **Monaco:** define custom themes `workbench-dark` / `workbench-light` based on
  `vs-dark` / `vs` with: `editor.background` = `--surface-panel`,
  `editor.lineHighlightBackground` = `--surface-hover`, `editorLineNumber.foreground` =
  `--text-tertiary`, `editorCursor.foreground` = `--accent`, selection =
  `--surface-selected`, syntax colors drawn from the ANSI palette above.
- **OnlyOffice:** the iframe is left light ("paper" doctrine). The hosting panel body is
  `--surface-paper-surround` with a 1px `--border-paper-rim`; our loading spinner /
  empty state inside a document panel uses `--text-on-paper` colors so nothing dark-mode
  flashes against the white canvas. Pass OnlyOffice `uiTheme: "theme-light"` always;
  never attempt to dark-skin the document itself.

---

## 3. Typography

Bundled via @fontsource — no runtime CDN:
`@fontsource/ibm-plex-sans` (400, 500, 600) and `@fontsource-variable/jetbrains-mono`.

- `--font-ui`: `"IBM Plex Sans", "Segoe UI", system-ui, sans-serif`
- `--font-mono`: `"JetBrains Mono Variable", "JetBrains Mono", "Cascadia Mono", Consolas, monospace`

IBM Plex Sans: designed for enterprise/technical UIs, real tabular figures, more
character than Inter without losing neutrality. JetBrains Mono: best-in-class code
legibility at 13px, ligatures off by default in terminal.

| Token | Size / line-height | Weight | Use |
|---|---|---|---|
| `--type-xs` | 11px / 16px | 500, uppercase, `letter-spacing: 0.04em` | Badges, section labels, tab-strip labels |
| `--type-sm` | 12px / 18px | 400 | File tree, tool-call rows, metadata, tab titles |
| `--type-md` | 13px / 20px | 400 | Base UI: buttons, inputs, lists, menus |
| `--type-chat` | 14px / 22px | 400 | Chat message body |
| `--type-lg` | 16px / 24px | 600 | Panel/empty-state headings |
| `--type-xl` | 18px / 26px | 400 | QuickBar input |
| `--type-2xl` | 20px / 28px | 600 | Welcome/zero-state titles |
| `--type-code` | 13px / 20px | 400 (mono) | Monaco default, inline code |
| `--type-term` | 13px / 18px | 400 (mono) | xterm.js |

Weights: 400 body, 500 emphasis/labels, 600 headings/active states. Never 700+.
Numeric UI text always sets `font-variant-numeric: tabular-nums` (utility `.u-tabular`).

---

## 4. Spacing, radius, borders, elevation

**Spacing** — 4px base, dense scale: `--space-1..9` = 2, 4, 6, 8, 12, 16, 20, 24, 32px.
Panel body padding 12px; list row horizontal padding 8px; bar (tab/status) horizontal
padding 8px; chat column padding 16px.

**Radii:** `--radius-xs` 3px (badges, keycaps), `--radius-sm` 4px (buttons, inputs,
tabs), `--radius-md` 6px (cards, tool-call rows), `--radius-lg` 10px (chat bubbles,
popovers), `--radius-xl` 12px (QuickBar), `--radius-full` 999px (pills, dots). Docked
panels themselves are square — radius belongs to floating things and insets, not the
dock grid.

**Borders:** 1px always; never 2px except the focus ring and tool-call status edge.
Structure = hairline + surface step, in that order of preference.

**Elevation** — three shadow levels, floating elements only:

| Token | Dark | Light |
|---|---|---|
| `--shadow-1` | `0 1px 2px rgba(0,0,0,0.4)` | `0 1px 2px rgba(27,31,38,0.08)` |
| `--shadow-2` | `0 4px 12px rgba(0,0,0,0.45)` | `0 4px 12px rgba(27,31,38,0.12)` |
| `--shadow-3` | `0 16px 40px rgba(0,0,0,0.55)` | `0 16px 40px rgba(27,31,38,0.18)` |

Level 1: tooltips, dragged tabs. Level 2: menus, popovers. Level 3: QuickBar, modals.
Every shadowed element also carries a 1px `--border-default` (dark-mode shadows alone
don't separate surfaces).

---

## 5. Motion

| Token | Value | Use |
|---|---|---|
| `--duration-1` | 80ms | Hover/active washes, color changes |
| `--duration-2` | 140ms | Tooltips, dropdowns, QuickBar in, badge changes |
| `--duration-3` | 200ms | Modals, panel-level fades, toasts |
| `--ease-standard` | `cubic-bezier(0.2, 0, 0, 1)` | Everything entering/changing |
| `--ease-exit` | `cubic-bezier(0.4, 0, 1, 1)` | Exits — always ≤ the entry duration |

**Animate only** `opacity` and `transform` (plus `background-color`/`border-color` at
`--duration-1`). GPU-cheap by construction.

**Never animates:** dockview sash drag and panel resize (1:1 with pointer), terminal
output, editor scrolling/content, tab activation (content swap is instant), chat
autoscroll during streaming, file-tree expand/collapse (instant), theme switch.

**Signature motion:** `--agent-working` dot pulses opacity 1 → 0.35 → 1 over 2s,
`ease-in-out`, infinite. This is the only looping animation in the app.

**Reduced motion:** under `prefers-reduced-motion: reduce`, all durations drop to 1ms
and the working-dot pulse stops (steady dot). Provided globally in tokens.css.

---

## 6. Component specs

### 6.1 Dockview tab bar + panel chrome
- Tab strip: height **34px**, bg `--surface-app`, bottom hairline `--border-subtle`.
- Tab: 12px/500 text, padding 0 12px, min-width 90px, max-width 200px with truncation.
  Inactive: `--text-tertiary`, transparent bg; hover: `--text-secondary` +
  `--surface-hover`. Active: `--text-primary`, bg `--surface-panel`, **no bottom
  border** (tab merges into panel body) — this fusion is the active indicator; no
  underline, no accent bar.
- Dirty dot: 6px `--text-secondary` circle replacing close button until hover.
- Close button: 16px glyph in 20px hit area, visible on hover/active only.
- Focused panel (keyboard focus lives inside it): its tab text `--text-primary` and a
  1px `--accent` top edge on the tab strip of that group only — the one place chrome
  uses accent structurally.
- Drop hints during drag: overlay `--accent-muted` fill + 1px `--accent` border.
- Document (Office) panels: body `--surface-paper-surround`; page shadow `--shadow-1`;
  rim `--border-paper-rim`.

### 6.2 File tree rows
- Row height **26px**, full-row hit target, 12px/400 text, indent 16px/level.
- Default `--text-secondary`; hover `--surface-hover`; selected `--surface-selected` +
  `--text-primary`; focus ring inset when keyboard-navigating.
- Chevron 12px, `--text-tertiary`, rotates 90° in 80ms (the one tolerated tree motion —
  transform, not layout). File-type icons 16px, single-color `--text-tertiary` (Lucide
  strokes; no colored icon soup).
- Git/agent-modified markers: 6px dot right-aligned, semantic colors.

### 6.3 Chat: messages, tool calls, permission prompts
- Column max-width **760px**, centered in panel, 16px side padding, 14px/22px body.
- **User message:** bubble on `--surface-elevated`, `--radius-lg`, padding 8px 12px,
  right-aligned, max-width 85%.
- **Assistant message:** no bubble — full-width text on `--surface-panel` (documents
  read better than chat toys). 8px between blocks; code blocks on `--surface-terminal`
  with `--radius-md` + `--border-subtle`, mono 13px.
- **Tool-call row:** collapsed height 28px, mono 12px `--text-secondary`; 2px left
  border in status color (`--agent-working` pulses via the dot, border steady;
  `--success` / `--error` when settled); chevron expands to output block (instant).
- **Permission prompt:** card on `--surface-elevated`, 1px `--warn`-tinted border
  (`--warn-bg` background wash at header). Buttons 28px height, `--radius-sm`, 13px/500:
  *Allow* = filled `--accent` / `--text-on-accent`; *Allow always* = outline
  `--border-default` text `--text-primary`; *Deny* = ghost `--text-secondary`, hover
  `--error` text. Never a red filled button — denying is safe, not destructive.
- Session header per agent: 11px uppercase label + status badge (§6.4), sticky.

### 6.4 Status badges
- Pill: height 20px, padding 0 8px, `--radius-full`, 11px/500 uppercase 0.04em.
- Anatomy: 6px status dot + label, bg = status tint (e.g. `--agent-working` at 14%
  alpha, provided as `--agent-*-bg` tokens), text = status color.
- Dot-only variant (10px) for tab strips and the tray; must always carry an
  `aria-label`/tooltip — color is never the only signal (icon or label accompanies it).

### 6.5 QuickBar (Ctrl+K)
- Overlay: width **640px**, max-height 60vh, top offset 15vh, `--surface-overlay`,
  `--radius-xl`, `--shadow-3`, 1px `--border-default`; scrim `--backdrop` (no blur —
  many-panel windows keep compositing cheap).
- Input row: height 52px, 18px/400 text, no border — separated from results by a
  hairline. Placeholder `--text-tertiary`.
- Result rows: height **40px**, 13px title + 12px `--text-tertiary` detail right-aligned;
  selected row `--surface-selected` with 2px `--accent` left edge; category headers 11px
  uppercase `--text-tertiary`.
- Keycap hints: 11px mono on `--surface-elevated`, 1px `--border-default`,
  `--radius-xs`, padding 1px 5px.
- Motion: fade + scale 0.98→1 in 140ms `--ease-standard`; exit 100ms fade.

### 6.6 Terminal panel
- Bg `--surface-terminal` (deepest surface — the terminal reads as a "well"), no padding
  compromise: 8px inset all sides. Font `--type-term`, ligatures off.
- ANSI palette from §2.7; cursor `--accent`, block, blink off by default; selection
  `--surface-selected`. Scrollbar: 10px overlay, thumb `--border-strong`, transparent
  track.
- Terminal tabs reuse §6.1 at the same 34px; a running-process dot uses
  `--agent-working` steady (no pulse — pulse is reserved for agents).

### 6.7 Status bar
- Height **24px**, bg `--surface-app`, 1px `--border-subtle` top hairline, 11px text.
- Left: workspace name (`--text-secondary`, 500) + active file path + 6px dirty dot
  (`--text-secondary`, same language as tab dirty dots).
- Centre: one chip per live session — 18px pill, 6px status dot (§2.6) + short title,
  click opens the session; the active one on `--surface-selected`. Beyond four chips the
  rest collapse to a `+N` count.
- Right: needs-attention count, working count (pulsing dot), last turn cost in mono
  tabular figures. Counts hide at zero — a quiet bar means nothing needs you.
- Every dot-only element carries an `aria-label` (§6.4).

### 6.8 Keymap and pass-through policy
Every binding lives in the command registry (`ui/src/commands.ts`); the QuickBar lists
the same registry, so nothing is reachable only by chord and nothing only by mouse.

| Chord | Command |
|---|---|
| `Ctrl+P` / `Ctrl+K` | Go to file |
| `Ctrl+Shift+P` | Show all commands (QuickBar, command mode) |
| `Ctrl+S` | Save file |
| `Ctrl+PageDown` / `Alt+PageDown` | Next editor tab |
| `Ctrl+PageUp` / `Alt+PageUp` | Previous editor tab |
| `Alt+W` / `Ctrl+F4` | Close editor tab |
| `Ctrl+1..4` | Focus Files / Editor / Agent / Terminal |
| `Alt+1..9` | Jump to the n-th most recent session |
| `Alt+T` | New terminal |

**Pass-through:** inside xterm and Monaco — both full keyboard applications — only
chords carrying `Alt` or `Ctrl+Shift` are intercepted; everything else reaches the
surface (`Ctrl+K` kills a line, `Ctrl+P` walks shell history). Plain keys are never
intercepted anywhere. Hence the Alt twins above: they are the ones that work from
inside a terminal or editor.

One exception, in the editor only: `Ctrl+P` and `Ctrl+K` are intercepted. Monaco
standalone leaves `Ctrl+P` unbound and uses `Ctrl+K` only as a fold-chord prefix, so
passing them through reaches the *browser* (print dialog, address bar), not an editor
command — go-to-file is the better owner. The terminal keeps both: xterm genuinely
uses them.

**Browser-reserved:** `Ctrl+Tab`, `Ctrl+W`, `Ctrl+T` never reach the page in a browser
tab, which is why cycling is PageUp/PageDown. `Ctrl+F4` is Chromium's alias for
`Ctrl+W` and is equally unstoppable, so closing is `Alt+W` — the chord the QuickBar
advertises and the only one that also works with focus inside Monaco or xterm.
`Ctrl+F4`, `Ctrl+1..4` and `Ctrl+PageUp/PageDown` are consumed by the browser in a dev
browser tab but arrive normally in the Tauri shell; they are listed as secondaries, not
as the advertised binding.

### 6.9 Empty states
- Centered, max-width 260px. Icon 32px, 1.5px stroke, `--text-tertiary`. Title 14px/600
  `--text-secondary`; hint 12px `--text-tertiary`; optional single action as `--accent`
  link or one outline button — never a filled button in an empty state.
- Always name the shortcut: "Open a file — Ctrl+P" with keycap styling (§6.5).
- Document panels' empty/loading states render on paper colors (§2.8), not panel colors.

---

## 7. Accessibility

- Contrast: body text ≥ 4.5:1 on its surface; large text (≥18.66px/600) and UI glyphs
  ≥ 3:1. The token pairs in §2 meet this — verify any new pair before adding it.
- Focus: 2px `--focus-ring` outline, offset 2px (inset for list rows), on **every**
  focusable element. Never `outline: none` without a replacement in the same rule.
- Hit targets ≥ 24×24px (desktop pointer, WCAG 2.2); rows may be 26px tall but must be
  full-width targets.
- Color never the sole signal: status dots pair with labels/tooltips; error borders pair
  with message text; diff/PnL colors pair with +/− signs.
- `prefers-reduced-motion` honored globally (§5). `prefers-color-scheme` sets the
  default theme; user override persists via `data-theme` on `<html>`.
- All icon-only buttons carry `aria-label`; QuickBar and menus fully keyboard-operable
  with visible selection.
