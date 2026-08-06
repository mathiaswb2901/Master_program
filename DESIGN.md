# Workbench Design System

Binding spec for all UI in Workbench (Tauri + React, dockview layout, Monaco, xterm.js,
OnlyOffice, agent chat, QuickBar). Every color/size named here exists as a CSS custom
property in `ui/src/design/tokens.css`. Components consume tokens — never raw hex.

Style direction: **ANVIL — a true-black instrument with exactly one hot amber.**

The window is a machined frame with content cut into it. Chrome is the *lighter*
surface and every buffer is a well sunk below it, six achromatic steps from #393939
chrome down to a pure-black terminal — the inverse of every graphite editor, and the
reason a panel here reads as an object instead of as more window. Nothing in the
neutrals carries a hue: Lab C\* is **0.00** on all twenty-three of them, no blue-grey, no warm
grey, no tint at all. Against that, one amber, and it means two things only: *where I am*
and *this is changing right now*. So the only chromatic pixel in the chrome is always the
one worth looking at.

Chosen 2026-08-06 from six directions rendered against a measured crispness bar
(ROADMAP change request). It is the one that answers what was actually wrong, which was
measurable rather than a matter of taste: five surfaces spanning 1.31:1 end to end (one
grey field), a `--border-subtle` at 1.17:1 on panel — below the threshold at which an
edge is perceived at all, while §1.4 elects hairlines to carry every piece of structure —
`--text-tertiary` at 4.24:1 under 11px text (a live WCAG failure against §7's own rule),
and a steel-blue accent at 220° on a 220° graphite base, which is VS Code's and GitHub's.

Deliberately rejected: the graphite-and-steel-blue frame this replaces; glassmorphism
(GPU cost in a many-panel window); any neutral carrying a hue; and a light theme derived
by inverting these values rather than re-deriving them from the same four rules (§2.8).

---

## 1. Principles

1. **The chrome frames; the content is sunk into it.** Value states depth, and the
   deepest thing on screen is what you are working in. Chrome is one or two steps
   *lighter* than the panel it holds, and the panel is lighter than the buffer inside
   it — `#393939` frame, `#202020` panel, `#141414` code, `#000000` terminal. The rule
   is direction-free, so it survives the light theme intact: content sits at the end of
   the value scale and every layer of frame steps one notch toward the middle (§2.1).
2. **Paper is a first-class surface, and paper needs a mat.** A white Office page inside
   this window is a feature, not a bug — but dropped straight onto chrome it reads as a
   hole. Document panels get a mat: a mid-grey surround (`--surface-paper-surround`, a
   full 6.9:1 below the page in dark), the page inset into it, a rim
   (`--border-paper-rim`) and the tab strip painted at the mat value, so the frame is one
   continuous surface from tab to page edge. Both themes.
3. **One amber, and it only ever means two things.** `--accent` marks **where I am**
   (the focused pane's live rule, selection, the field you are typing in, the drop
   target under the pointer) and **this is changing right now** (an agent working, a
   value moving, the one action the app is blocked on). Nothing standing, nothing
   decorative, nothing that is still true when you look away. Everything else in the
   chrome is achromatic, which is what makes a single amber pixel carry information at a
   glance instead of being a colour scheme.
4. **Hairlines, not shadows, define structure.** Panels separate with 1px borders and
   surface steps. Shadows exist only on things that float (menus, QuickBar, tooltips).
   A corollary that had been left implicit and cost the app its structure: if a hairline
   is the *only* thing carrying a boundary, it has to be visible. `--border-subtle` is
   2.51:1 on `--surface-panel` in both themes, and `ui/e2e/palette.test.ts` fails the
   build if it drifts below.
5. **Density is respect.** 13px base UI, 26px rows, 34px bars. Analysts want more on
   screen, not bigger buttons. Minimum hit target 24×24px (desktop, WCAG 2.2).
6. **Motion is continuous, and it is restrained by damping rather than by absence.**
   Chrome moves on critically damped springs — crisp arrival, no overshoot — and only on
   `transform`/`opacity`/colour. Anything tracking a pointer or a key moves 1:1 with zero
   animation, and content never fades. §5 is binding and states every case.
7. **Numbers are data.** Anything numeric (prices, sizes, times, line numbers) renders
   in tabular figures (`font-variant-numeric: tabular-nums`) or the mono font.
8. **Keyboard-first.** Every interactive element has a visible 2px focus ring; every
   surface is reachable without a mouse; QuickBar (Ctrl+K) can reach anything.

---

## 2. Color system

**ANVIL.** Two rules produce almost all of it, and both are measured rather than
asserted — `ui/e2e/palette.test.ts` re-derives every figure below from `tokens.css` and
fails the build when one drifts.

1. **The neutrals have no hue.** Every grey in the system is Lab C\* = **0.00** — the
   surfaces, the text, the borders, the ANSI black/white slots, the shadows and the
   scrim. The palette this replaces sat at hue 220° with C\* 2.2–4.1, which is why the
   accent had to be a blue to look like it belonged.
2. **Value states depth, and the content is the deepest thing on screen.** Six surfaces,
   adjacent steps 5.8–6.3 L\*, 29.7 L\* end to end. Chrome is *lighter* than the panel it
   frames; the panel is lighter than the buffer inside it.

Against that: one amber, spent only on *where I am* and *what is changing right now*
(§2.4). Semantic colours keep the hues everyone already reads (green/orange/red) and one
orchid for "needs you", all ≥ 4.5:1 as text on their own surfaces and ≥ 3:1 as
indicators.

### 2.1 Surfaces

The ramp, deepest well first. `L*` is the dark value; light's is in brackets.

| Token | Dark | Light | L\* | Use |
|---|---|---|---|---|
| `--surface-terminal` | `#000000` | `#FFFFFF` | 0.0 [100.0] | xterm ground — the deepest well |
| `--surface-code` | `#141414` | `#F3F3F3` | 6.3 [95.8] | Monaco buffer + gutter, chat code blocks, tool output, diffs |
| `--surface-panel` | `#202020` | `#E8E8E8` | 12.3 [92.0] | Panel bodies: file tree, chat, usage, nested tab strips |
| `--surface-elevated` | `#2C2C2C` | `#DDDDDD` | 18.0 [88.1] | Cards, inputs, user chat bubble, keycaps |
| `--surface-app` | `#393939` | `#D1D1D1` | 24.0 [83.8] | Window chrome: pane tab strips, status bar, the dock's own background |
| `--surface-overlay` | `#464646` | `#C6C6C6` | 29.7 [79.9] | Everything that floats: QuickBar, modal, menus, popovers, toasts, tooltips |
| `--surface-paper` | `#FFFFFF` | `#FFFFFF` | 100.0 | Document canvas ("paper") — identical in both themes |
| `--surface-paper-surround` | `#5A5A5A` | `#A5A5A5` | 38.2 [67.7] | The mat a docked page sits on (§6.1). 6.90:1 [2.46:1] below the page |
| `--surface-hover` | `rgba(255,255,255,0.06)` | `rgba(0,0,0,0.06)` | — | Hover wash on rows/tabs/buttons |
| `--surface-active` | `rgba(255,255,255,0.10)` | `rgba(0,0,0,0.10)` | — | Pressed wash |
| `--surface-selected` | `rgba(251,191,36,0.18)` | `rgba(251,191,36,0.48)` | — | Selected row/tab/list item — an amber wash, in both themes |
| `--backdrop` | `rgba(0,0,0,0.60)` | `rgba(0,0,0,0.35)` | — | Modal/QuickBar scrim |

**`--surface-app` is lighter than `--surface-panel`, and that is the whole direction.**
The chrome is a frame with wells cut into it. Three consequences you apply without
asking, because a token swap alone puts them the wrong way round:

- **A nested tab strip is painted `--surface-panel`, not `--surface-app`.** The editor's
  file strip and the terminal's shell strip live *inside* a pane whose own strip is
  already `--surface-app`; painting them at chrome value would put the lightest surface
  in the window underneath the pane's dark active tab and break the fusion that is the
  active indicator (§6.1). The window descends `#393939` pane → `#202020` strip →
  `#141414` buffer, or `#000000` for a terminal.
- **A tab fuses with the surface immediately below it**, which for the editor's file
  strip is `--surface-code`, for the terminal's is `--surface-terminal`, and for a
  document is the mat.
- **The sash hairline is `--border-strong`, not `--border-subtle`.** It is drawn in the
  dock's `--surface-app` gap rather than on a panel, where the subtle hairline's 2.51:1
  becomes 1.78:1 and disappears. Same rule, measured where it actually sits.

### 2.2 Text

Contrast on `--surface-panel`; both themes are within 0.03 of each other.

| Token | Dark | Light | On panel | Use |
|---|---|---|---|---|
| `--text-primary` | `#F5F5F5` | `#151515` | 14.94:1 [14.90] | Headings, active tab, primary content |
| `--text-secondary` | `#D6D6D6` | `#2D2D2D` | 11.21:1 [11.24] | Body in panels, inactive-but-relevant |
| `--text-tertiary` | `#B4B4B4` | `#454545` | 7.86:1 [7.83] | Metadata, timestamps, inactive tabs, placeholders |
| `--text-disabled` | `#858585` | `#6A6A6A` | 4.42:1 [4.41] | Disabled controls only (exempt from §7's floor) |
| `--text-on-accent` | `#141414` | `#141414` | 11.04:1 on `--accent-fill` | Text on amber-filled buttons |
| `--text-on-paper` | `#141414` | `#141414` | 18.1:1 on paper | Our chrome drawn over paper surfaces |

`--text-tertiary` is the token the diagnosis turned on: it was 4.24:1 under 11px text,
which §7 forbids and which is most of why the window read as washed out. It now carries
the same 7.86:1 in both themes.

### 2.3 Borders

Structure lives here (§1.4), so these are contrast figures, not shades.

| Token | Dark | Light | On panel | Use |
|---|---|---|---|---|
| `--border-subtle` | `#5E5E5E` | `#939393` | 2.51:1 | Panel-to-panel hairlines, row separators |
| `--border-default` | `#6E6E6E` | `#818181` | 3.20:1 [3.18] | Inputs, cards, buttons |
| `--border-strong` | `#8C8C8C` | `#646464` | 4.85:1 [4.83] | Hover/active borders, sash hairlines, dividers needing weight |
| `--border-paper-rim` | `#9E9E9E` | `#555555` | 6.08:1 | 1px rim framing document ("paper") panels |
| `--focus-ring` | `#FBBF24` | `#856000` | 9.76:1 [4.67] | 2px focus outline everywhere |

The old `--border-subtle` was **1.17:1** on panel — below the threshold at which a 1px
edge is perceived at all. That single number is the mechanical reason no panel read as an
object, and it is why §1.4 now carries a contrast floor rather than only a preference.

### 2.4 Accent — the amber, and where it is allowed

| Token | Dark | Light | What it is |
|---|---|---|---|
| `--accent` | `#FBBF24` | `#856000` | The amber that **marks**: rules, text, borders, dots, the focus ring |
| `--accent-hover` | `#FFD24D` | `#6E5000` | Its hover |
| `--accent-active` | `#E0A413` | `#5A4100` | Its press |
| `--accent-muted` | `rgba(251,191,36,0.16)` | `rgba(251,191,36,0.42)` | The amber **wash** — carries `--text-primary`, never amber text |
| `--accent-fill` | `#FBBF24` | `#FBBF24` | The amber **area** a label sits on. One value for both themes |
| `--accent-fill-hover` | `#FFD24D` | `#FFD24D` | |
| `--accent-fill-active` | `#E0A413` | `#E0A413` | |

`--accent` is 9.76:1 on `--surface-panel` (dark) and 4.67:1 (light) — usable as text in
both — and ≥ 3:1 on every chrome surface, which is what a 2px rule needs (WCAG 1.4.11).

**The two meanings, and nothing else.** *Where I am*: the focused pane's live rule
(§6.1), a selected row, the field you are typing in, the drop target under the pointer,
the sash you are dragging, the focus ring. *Changing right now*: an agent working, a
meter filling, focus mode holding the window, the one action the app is blocked on
(`Allow`, `Approve` — `--accent-fill`).

**Demoted when ANVIL landed**, each because it was still true when you looked away:

| Was amber | Now | Why |
|---|---|---|
| Links in chat and the provenance bar | `--text-primary` + underline | Navigation, not motion. The underline is also the non-colour signal §7 asks for |
| Tool-call and plan-file hover | `--text-primary` | A hover is answered by the wash; it does not need the one colour that means "live" |
| The QuickBar input's focus underline | `--border-strong` | The bar being open already says focus is in it; the selected row's amber edge is the mark that matters inside a 640px overlay |
| The plan card's *Recommended* pill | Outlined neutral | A standing property of an option — true before you opened the card |
| The "this really is Word" dot | `--success` | True for as long as the window is docked. A permanent amber dot is how amber stops meaning anything |
| Every live shell tab's dot (§6.6) | `--success` | Found by *looking at the running app*, not by reading the stylesheet: it is set from `terminal.alive`, so a shell sitting at a prompt all afternoon wore the one "right now" colour, on every terminal tab at once |

**Kept, deliberately**: the scene-graph `accent` role in visual artifacts
(`.is-accent` on a table row, a node, a metric). That is the model saying *this is the
number that matters in this artifact* — content, not chrome, and the one place amber is
allowed to be a standing mark.

### 2.5 Semantic

| Token | Dark | Light | On panel | Tinted bg (both) |
|---|---|---|---|---|
| `--success` | `#22C55E` | `#03722C` | 7.15:1 [4.97] | `--success-bg` at 16% |
| `--warn` | `#FF7A18` | `#A44804` | 6.24:1 [4.89] | `--warn-bg` at 16% |
| `--error` | `#FF4D4D` | `#C70505` | 4.98:1 [4.96] | `--error-bg` at 16% |
| `--info` | `#E879F9` | `#A905C2` | 6.62:1 [4.91] | `--info-bg` at 16% |

Hues are identical across themes (142° / 25° / 0° / 292°); only the value moves. `--warn`
is the nearest neighbour to the amber by construction — orange beside amber — and is
held ≥ ΔE 25 from it, tested. It is also rare: a conflict bar, a usage warning.

### 2.6 Agent status

Dedicated tokens (do not reuse semantic tokens directly, even where values match — the
meaning is different and may diverge later).

| Token | Dark | Light | Meaning |
|---|---|---|---|
| `--agent-working` | `#FBBF24` | `#856000` | Agent running — dot pulses (§5.4). The canonical "changing right now" |
| `--agent-attention` | `#E879F9` | `#A905C2` | Needs permission / user input — dot steady |
| `--agent-idle` | `#7A7A7A` | `#6A6A6A` | Session open, nothing running |
| `--agent-done` | `#22C55E` | `#03722C` | Finished since last viewed |
| `--agent-error` | `#FF4D4D` | `#C70505` | Failed / crashed |

`--agent-attention` is **not** amber, and the swap is deliberate: a session waiting on
you is a standing state that can sit on screen for an hour, and a standing amber spends
the one colour that has to mean motion. Orchid is unmistakable beside a green, an orange
and a red, and it is the same hue as `--info`.

### 2.7 Terminal ANSI palette

Retuned for a `#000000` ground — the previous set was chosen against `#111317` and its
darkest slots simply vanished on true black. Three rules, all tested:

1. Every slot is ≥ 4.5:1 on `--surface-code`, which is where Monaco reads them; the dim
   `black` slot is ≥ 2:1 on `--surface-terminal` (it is a background, never body text).
2. `black`, `white`, `bright-black` and `bright-white` are pure greys (C\* = 0).
3. **No slot comes within ΔE 25 of `--accent`.** The yellow is the one this binds: it is
   pushed to hue 57° (dark) / 72° (light), ΔE 26.8 / 29.5, because a warm yellow at
   terminal scale reads as the focus rule and then amber has stopped meaning anything.

Dark: black `#4D4D4D`, red `#FF6B60`, green `#3ECF7F`, yellow `#D8D24F`, blue `#6BB6FF`,
magenta `#C68BF5`, cyan `#45D6DE`, white `#C8C8C8`; brights `#8A8A8A #FF9A90 #6FE3A4
#EDE783 #9CD1FF #DFB6FB #7FE8EE #F5F5F5`. Light: black `#2D2D2D`, red `#B02419`, green
`#0E6B36`, yellow `#556B00`, blue `#14539E`, magenta `#7A2E9E`, cyan `#12666E`, white
`#545454`; brights `#454545 #8A1A11 #0A5028 #404E0A #0D3E78 #5C2277 #0D4B52 #151515`.

`bright-black` moved furthest (`#545B66` → `#8A8A8A`): Monaco maps `comment` to it, so it
is body text and was failing at 2.9:1. It is now 5.34:1 on the buffer. Feed these to
xterm.js `theme` and to the Monaco theme's token colors so terminal, editor and chrome
read as one system.

### 2.8 Deriving the light theme, and harmonizing Monaco / OnlyOffice

**The light theme is derived, not inverted.** ANVIL is a dark direction; light re-applies
its four rules to a white ground:

- *Achromatic neutrals* — C\* = 0.00 on all twenty-three, shadows and scrim included.
- *Content at the end of the scale* — so the chrome is **darker** than the wells here.
  Same sentence, opposite direction; §2.1's three consequences hold unchanged.
- *Visible edges* — `--border-subtle` is 2.51:1 on panel, dark's exact figure, because
  the threshold is a ratio and not a value.
- *One amber* — with the one real problem in the whole exercise, stated rather than
  papered over.

**The hot amber cannot be a mark on a light ground.** `#FBBF24` is 1.33:1 on
`--surface-panel` in light: not text, not a rule, not a border, not even close to the 3:1
a UI indicator needs. It is a *light* colour, and the value scale has no room above it.
The lightest amber at that hue which clears 4.5:1 as text is `#856000`, and that is what
light spends on every mark. The hot amber survives where it is a **filled area** —
`--accent-fill`, carrying `#141414` text at 11.04:1 — which is why `--accent-fill` is one
value in both themes and `.wb-btn-primary` reads for it rather than for `--accent`.

The one honest difference between the themes: light's surface steps are ~4 L\* against
dark's ~6, and 20.1 end to end against 29.7. A light ramp starts at 100 and has nowhere
to go but down; matching dark's spread would land the chrome in mid-grey and it would
stop being a light theme. Light pays it back in its borders, which are far easier to see
against a bright field.

- **Monaco:** custom themes `workbench-dark` / `workbench-light` on `vs-dark` / `vs`
  with `editor.background` and `editorGutter.background` = **`--surface-code`** (the
  buffer is a well, one step below the panel and the tree beside it),
  `editor.lineHighlightBackground` = `--surface-hover`, `editorLineNumber.foreground` =
  `--text-tertiary`, `editorCursor.foreground` = `--accent`, selection =
  `--surface-selected`, syntax colors drawn from §2.7.
- **OnlyOffice:** the iframe is left light ("paper" doctrine). The hosting panel body is
  `--surface-paper-surround` with the page inset into it behind a 1px
  `--border-paper-rim`; our loading spinner / empty state inside a document panel uses
  `--text-on-paper` colors so nothing dark-mode flashes against the white canvas. Pass
  OnlyOffice `uiTheme: "theme-light"` always; never attempt to dark-skin the document.
  The native Office host takes the same inset, and its rim is an *outset* ring rather
  than a border — the host sizes the real Word window from that element's border box, so
  a 1px border would be underneath the window and invisible.

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

> Revised 2026-08-05. The previous version capped motion at 80–200ms, called motion
> "confirmation, never entertainment", and forbade animating tab activation, panel
> resize, tree expand/collapse and the theme switch. Half of that was right and is kept
> — a workbench full of dense data must not wobble. The other half is what made the app
> read as *static and cheap*: a window where nothing ever moves does not read as fast, it
> reads as a screenshot. What follows replaces it and is binding.

### 5.1 Doctrine

1. **Motion is continuous, not scheduled.** Chrome moves on a **spring**, not on a fixed
   duration with an eased curve. A spring is interruptible by construction: it has a
   position and a velocity at every instant, so a second input mid-flight resolves into
   the first instead of queueing behind it. This is the single thing that separates
   "alive" from "animated".
2. **Restraint is in the damping, not in the duration.** Every chrome spring is
   **critically damped** (`bounce = 0`): the fastest arrival that never crosses its
   target. No wobble, no settle, no overshoot on anything a professional reads numbers
   off. Exactly one spring in the app overshoots, and §5.4 says where it is spent.
3. **Two channels, and only two.** *Travel* is `transform`. *Tint* is `opacity` and
   colour. Nothing else is ever animated — see §5.5. A third channel would be layout,
   and animating layout is how an app becomes janky.
4. **The pointer is never animated.** Anything tracking a pointer — sash drag, panel
   resize, scroll, drag-and-drop — moves 1:1 with zero interpolation. So does anything
   tracking a key: list selection, tree navigation, tab cycling. A highlight that eases
   behind the arrow key *is* the feeling of lag.
5. **Instant in, eased out.** Hover and press respond on the frame the input arrives
   (`transition-duration: 0s` on `:hover`/`:active`); only the decay is animated. A
   control that fades *up* under the cursor feels slow no matter how short the fade.
6. **A quiet window.** Nothing animates on its own except one 2s pulse (§5.4). If
   something is fading and the user did not cause it and no agent changed state, it is a
   bug — the dockview tab-strip scrollbars, which fade over a full second on every
   resize upstream, are overridden to nothing for exactly this reason.
7. **Content never fades.** The chrome around a buffer may move; the buffer may not.
   Editor content, terminal output, a tab's content swap and file-tree rows appear at
   once, because motion on them is time added before the user can read.

### 5.2 The springs, and where they come from

A spring is a second-order system, so its tokens are **derived**, not drawn. The
derivation lives in `ui/src/design/springs.ts`; `ui/e2e/perf/motion.test.ts` fails the
build if `tokens.css` and the derivation disagree. To retune: change a spec there, run
`npm run test` in `ui/`, paste the block it prints. Never hand-edit a curve point.

Parameterised the way SwiftUI's `Spring(duration:bounce:)` is, because stiffness and
damping are not numbers a designer holds in their head — with mass 1:

```
omega0 = 2*pi / duration      k = omega0^2      zeta = 1 - bounce      c = 2*zeta*omega0
```

CSS has no spring easing, so the response is sampled into a `linear()` polyline. For a
fixed damping ratio the response is the **same curve in normalised time** whatever the
duration — so there are two easing tokens and four durations, not four easings.

| Token | What it is |
|---|---|
| `--ease-spring` | `bounce 0` → ζ = 1, critical damping. Every piece of chrome. |
| `--ease-spring-bounce` | `bounce 0.3` → ζ = 0.7. Overshoots ~4.5%. One use (§5.4). |
| `--spring-snap-ms` | 190ms — `duration 0.14s`. Chip, badge, glyph, tool row. |
| `--spring-base-ms` | 300ms — `duration 0.22s`. The default: overlays, menus, focus mode. |
| `--spring-bounce-ms` | 590ms — `duration 0.4s`. The celebratory spring. |

Three durations, because three is how many distances the chrome currently travels. A
fourth is one line in `springs.ts` plus a row here, the day something needs it — a token
nothing uses is a token nobody has tuned.

A duration token is the **settling** time (1.35× the `duration` parameter), not the
perceived one: a critically damped spring is past 90% of its travel at under half of it.
That is why 300ms here does not feel like the old 200ms cubic-bezier — it feels quicker.

### 5.3 The channels

Compose these; never write a bare duration or easing in a component stylesheet.

| Token | Value | Channel | Use |
|---|---|---|---|
| `--motion-tint` | 110ms `--ease-tint` | tint | Hover decay, chip colour, wash |
| `--motion-tint-slow` | 180ms `--ease-tint` | tint | Panel-level fades, a tool row settling |
| `--motion-exit` | 120ms `--ease-exit` `both` | tint | Every dismissal |
| `--motion-move-snap` | `--spring-snap-ms` `--ease-spring` | travel | Small transforms |
| `--motion-move` | `--spring-base-ms` `--ease-spring` | travel | The default transform |
| `--motion-enter` | `--spring-base-ms` `--ease-spring` `both` | travel | Entrance keyframes |
| `--motion-celebrate` | `--spring-bounce-ms` `--ease-spring-bounce` `both` | travel | §5.4 |

**Travel distances are tokens too** — `--motion-rise` (6px), `--motion-lift` (10px),
`--motion-scale-in` (0.98), `--motion-zoom-in` (0.985). Nothing may hard-code a
translate or a scale, because §5.6 works by setting these to zero.

Entrances and exits are shared keyframes in `tokens.css`: `wb-rise-in` in, `wb-slide-out`
and `wb-fade-out` out. (An overlay centred with `translateX(-50%)` needs its own keyframe
so the entrance carries the centring — the QuickBar and the modal each have one.) An exit
is always shorter than its entrance and never springs: a dismissal that springs back is a
dismissal arguing with the user.

JavaScript-driven motion goes through `ui/src/motion.ts` and reads its numbers from
these tokens at the moment it uses them. That is what makes §5.6 work without a second
implementation of the rule. There is no other place in the app that may call `animate()`.
It also samples the dock's live `opacity` and `transform` before it replaces a running
animation, so §5.1.1 holds for the JS half too: a keyframed spring is not interruptible
for free the way a CSS one is, and restarting it from the fixed dip would pop the dock
backwards mid-gesture. `ui/e2e/motion.spec.ts` presses `Alt+M` twice inside one animation
and asserts the second starts where the first had got to.

### 5.4 What moves, and what does not

Every row is a decision with a reason. Add to this table before adding motion.

| Interaction | Moves? | How | Why |
|---|---|---|---|
| **Focus mode enter/exit** (`Alt+M`) | **Yes** | Whole dock: `--motion-zoom-in` → 1 with opacity 0.62 → 1, `--motion-move`. A press that interrupts one starts from where the dock is, not from the dip | Every other panel disappears at once; without motion the window teleports and the user has to re-find where they are. Animating the dock, not the grid dockview just resized, keeps it one composited layer. |
| **Layout switch** | **Yes** | Whole dock: opacity 0.45 → 1, `--motion-tint-slow`, **no zoom** | Panels are recreated, moved and resized — a zoom would claim they flew somewhere they did not, and animating their geometry would be animating layout. A short dip says "the window changed" and gets out of the way. |
| **QuickBar open/close** | **Yes** | `wb-qb-in` (scale `--motion-scale-in` + fade, `--motion-enter`); exit `wb-fade-out` at `--motion-exit` | It owns the screen while it is up. It used to unmount on the frame it closed, which read as a glitch rather than a dismissal. |
| **Toast enter/exit** | **Yes** | `wb-rise-in`; exit `wb-slide-out` | The one thing on screen the user did not ask for, so the one thing allowed to move to be noticed. |
| **A success toast** | **Yes**, and it is the only bounce | `--motion-celebrate` | The single celebratory moment the app has. An error that bounced would be flippant. |
| **Tool-row settle** | **Yes**, tint only | `border-color` at `--motion-tint-slow` | Working → succeeded is a colour change. A row that *moved* when it settled would shove every row below it, in the densest list in the app. |
| **Status chip / status dot changes** | **Yes**, tint only | `background-color`, `border-color`, `color` at `--motion-tint` | These change because a session did, not because the user did. Cross-fading is what makes the bar read as live instead of as numbers being overwritten. |
| **Tab activation** | **Yes**, tint only — *reversed from the old rule* | `.dv-tab` background + colour at `--motion-tint` | The tab merging into the panel body is the whole active indicator (§6.1); a merge that happens in zero frames is the "cheap" tell. The **content swap underneath stays instant** — a panel you cannot read yet is worse than no animation. |
| **Tree expand/collapse** | **Partly** | Chevron rotates on the travel channel; rows appear at once | Animating row insertion means animating height on up to 2,000 rows — the exact layout thrash §5.5 forbids, on the exact surface the perf lane already measures. |
| **Menus and popovers** | **Yes** | `wb-rise-in` from the edge they belong to | They arrive from the control that summoned them. |
| **Sash drag, panel resize** | **No** | 1:1 | §5.1.4. |
| **Terminal output, editor content and scrolling** | **No** | — | §5.1.7. |
| **List/tree selection, QuickBar row selection** | **No** | — | It tracks a key. See §5.1.4. |
| **Chat autoscroll** | **No** | — | Not restraint — mechanism. The chat pins to the bottom by comparing `scrollHeight − scrollTop − clientHeight`; a smooth scroll makes that gap large while it catches up, so the pin releases and the view stops following the stream. A smooth *jump to bottom* is fine, and belongs with the jump-to-bottom control that does not exist yet. |
| **Theme switch** | **No**, and it is suppressed on purpose | §5.7 | — |
| **Agent "working" dot** | **Yes** | opacity 1 → 0.35 → 1 over 2s `ease-in-out`, infinite | The only looping animation in the app. |

### 5.5 What may be animated

**Only** `transform`, `opacity`, `background-color`, `border-color`, `color`,
`outline-color`. Never `width`, `height`, `top`, `left`, `margin`, `padding`,
`box-shadow`, `filter` — the first four trigger layout, which is the most common way an
app becomes janky, and the rest force paint on surfaces this app makes large.

Never `transition: all`: it animates whatever a later edit adds to the rule.

Never a static `will-change`. Motion here promotes a layer for the length of one
animation and hands it back; a permanent layer is memory paid on every frame.

`ui/e2e/perf/motion.test.ts` and `ui/e2e/perf/motion.spec.ts` enforce all of the above
against every stylesheet in `ui/src/` and against the production bundle, and pin the
third-party rules that break it in a ledger that must match exactly.

### 5.6 Reduced motion: zero the travel, keep the tint

`prefers-reduced-motion: reduce` is a **vestibular** setting, not a "no animation"
setting. Movement across the screen is the trigger. A colour settling or a panel fading
is not, and removing those costs the user the feedback the motion was carrying.

The rule, in full:

1. Every **travel distance** goes to zero (`--motion-rise`, `--motion-lift`,
   `--motion-scale-in`, `--motion-zoom-in`). Every shared entrance keyframe therefore
   degrades to a pure fade with no code path of its own.
2. Every **transform transition** goes to `0s` (`--motion-move*`).
3. Entrances **keep their duration**, at the tint channel's pace, as fades.
4. The **tint channel is untouched**. `--motion-tint`, `--motion-tint-slow` and
   `--motion-exit` are the same numbers as always.
5. The **working-dot pulse stops** (steady dot). It is the one tint animation that is
   also motion: it never resolves.
6. **Rotation in place of a ≤16px glyph is not travel** and may stay — but only when it
   is driven by the travel channel, which zeroes it anyway. Nothing gets an exemption by
   being small; the chevron's legacy duration token is zeroed by name in `tokens.css`
   until its stylesheet moves to the channels.

### 5.7 The theme switch never animates

Flipping `data-theme` changes the computed value of a colour on nearly every element in
the window. With colour transitions in the chrome — and §5.4 puts them on tabs, chips,
buttons, menu items and tool rows — that starts a transition on *each* of them: hundreds
of concurrent animations and a style recalculation with all of them live. Measured on
this app, an unguarded flip started 17 transitions from four visible panels.

So the flip is bracketed: `ui/src/motion.ts` sets `data-theme-switching` on `<html>`,
which carries `transition: none !important`, flips the attribute in the same task so the
browser folds both into **one** style recalculation, forces that recalculation with a
single computed-style read, and clears the bracket. Nothing has a value change left to
transition from afterwards. `ui/e2e/motion.spec.ts` counts the `transitionrun` events
the browser actually fires and requires zero.

### 5.8 Deprecated

`--duration-1/2/3` and `--ease-standard` remain as aliases onto the channels so
stylesheets written before this revision keep working. New work uses §5.3. They will be
removed once the last stylesheet has moved.

Each aliases onto the channel its consumers actually use — a deprecated name is not a
free pass past §5.1.3. `--duration-2/3` are travel durations, `--duration-1` a tint one,
and **`--ease-standard` is the tint ease, not the spring**: every stylesheet still on it
pairs it with a colour property, save the file tree's chevron, whose `--duration-1` had
already put it on the tint channel. Pointing the name at the spring instead would
re-curve other lanes' colour transitions without a character changing in their files.
A legacy consumer that wants travel migrates to `--motion-move*` in its own file, where
a reviewer can see it. `ui/e2e/perf/motion.test.ts` resolves every transition value
through the token file and fails the build if a colour-only one lands on a spring.

---

## 6. Component specs

### 6.1 Dockview tab bar + panel chrome
- **THE LIVE RULE — the signature.** The pane the keyboard is in wears a **2px
  full-bleed `--accent` rule across the top of its tab strip**, and exactly one pane in
  the window wears it. This is the app's focus indicator and the single most-seen use of
  the amber; everything else about a pane's chrome is neutral. Full-bleed because the
  *pane* is what is focused, not a tab. 2px rather than the 1px this used to be: §1.4
  gives every hairline in the window to structure, so a 1px focus mark borrows
  structure's weight and becomes something you have to look for. Implemented as a
  `::before` overlay on `.dv-active-group`'s tab container, and never animated — a focus
  indicator that arrives late is a focus indicator that lies. **Not** an `inset`
  box-shadow: that paints on the container's own background and the tabs are children
  with backgrounds of their own, so it is occluded across the whole width of the active
  tab and the rule appears to *start* after the tab instead of crossing the pane. Not a
  `border-top` either, which would cross but would take its 2px out of the 34px strip
  and jump the tab row every time focus moved.
- Tab strip: height **34px**, bg `--surface-app`, bottom hairline `--border-subtle`.
- **A nested strip is `--surface-panel`, not `--surface-app`** (§2.1). The editor's file
  strip and the terminal's shell strip sit inside a pane that already has a strip; they
  are one step further in, and painting them at chrome value would put the window's
  lightest surface under the pane's dark active tab.
- Tab: 12px/500 text, padding 0 12px, min-width 90px, max-width 200px with truncation.
  Inactive: `--text-tertiary`, transparent bg; hover: `--text-secondary` +
  `--surface-hover`. Active: `--text-primary`, bg = **the surface immediately below it**
  (`--surface-panel` for a pane tab, `--surface-code` for a file tab, `--surface-terminal`
  for a shell tab, the mat for a document), **no bottom border** — this fusion is the
  active indicator; no underline, no accent bar.
- Dirty dot: 6px `--text-secondary` circle replacing close button until hover.
- Close button: 16px glyph in 20px hit area, visible on hover/active only.
- **Panel** (not document) tabs are chrome: title, the tool's optional 14px glyph, and
  at most one dot-only badge (§6.4). No close button — except on a panel that is *not*
  in the startup layout, which one of its commands opened: that tab carries a close
  button in an 18px hit area, always visible, because it is the only way back and a
  hover-only affordance on the one closable tab is a dead end. Which panels those are
  is a registry fact (`openByDefault: false`), never a list in the tab component.
- Focused panel (keyboard focus lives inside it): its tab text `--text-primary`, plus
  the live rule above.
- Drop hints during drag: overlay `--accent-muted` fill + 1px `--accent` border. Amber
  is right here — it is the target under the pointer *now*.
- **Document (Office) panels get a mat, not a background** (§1.2). Four parts, and all
  four are needed:
  1. **Mat** — the panel body is `--surface-paper-surround`, a mid-grey 6.90:1 below the
     page in dark and 2.46:1 in light. Not a shade of the chrome: a page on chrome reads
     as a hole punched in the window.
  2. **Inset** — the page is inset `--space-5` on all sides, so it is a sheet *laid on*
     the mat rather than a panel that happens to be white.
  3. **Rim and lift** — 1px `--border-paper-rim` plus `--shadow-1`. On the native host
     the rim is an outset ring, because the real Word window covers the border box.
  4. **The tab strip is painted at the mat value** — this
     is the part usually forgotten, and it is what makes the frame one continuous
     surface from the tab down past the page edge instead of two unrelated greys
     meeting. The active document's tab fuses into the mat like any other tab fuses into
     what is below it. Driven by `is-document` on the editor frame, which follows the
     *active view* (`documentViewFor`), never a list of file extensions.
     Its labels are `--text-primary`/`--text-secondary`, **not** `--text-on-paper`: the
     mat is a mid-grey (L* 38 dark, 68 light), the one surface in the system where the
     app's text pair works (6.33:1 / 4.75:1) and the paper pair does not (2.67:1, worse
     than the failure this palette exists to fix). `--text-tertiary` is 3.33:1 there and
     is never used on the mat.
- Bars above the buffer (conflict, provenance): one line, 12px, 6px/12px padding,
  bottom hairline `--border-subtle`, background = the status wash for what they mean
  (`--warn-bg` for a conflict, `--agent-done-bg` for an agent change). Actions on the
  right as 24px ghost/outline buttons; a link inside the sentence is `--text-primary`
  with an underline (§2.4 — links were demoted off the amber), never a filled control.
  A tab whose file an agent changed carries the same 6px `--agent-done` dot
  as the tree row until the tab is brought forward.
- The provenance bar is **not** an unread marker and does not clear on open — the two
  dots do that. It answers "who wrote what I am reading", so it stands for as long as
  the attribution does (any later change from anywhere else retracts it) and it carries
  the only link back to that conversation. **Dismiss** ends it for that file, and the
  dismissal persists across reloads: one line, one click, once.

### 6.2 File tree rows
- Row height **26px**, full-row hit target, 12px/400 text, indent 16px/level.
- Default `--text-secondary`; hover `--surface-hover`; selected `--surface-selected` +
  `--text-primary`; focus ring inset when keyboard-navigating.
- Chevron 12px, `--text-tertiary`, rotates 90° on the travel channel — the one tree
  motion (§5.4): rows themselves appear at once, because animating row insertion is
  animating height on up to 2,000 of them. File-type icons 16px, single-color
  `--text-tertiary` (Lucide strokes; no colored icon soup).
- Agent-modified marker: 6px dot right-aligned, `--agent-done` (§2.6) — "an agent
  changed this and you have not looked yet". Carries an `aria-label`/tooltip naming
  the session, never colour alone (§6.4); it clears when the file is opened or its
  provenance bar dismissed. Git markers reuse the same slot in semantic colours.
- **Virtualised**: only the rows the panel can show (plus a small overscan) exist in
  the DOM, positioned by `index × 26px` inside a spacer of the full height. The row
  height above is therefore *arithmetic*, not only styling — changing `--row-height`
  means changing the constant it is asserted against (`ui/e2e/perf/rowGeometry.test.ts`).
- **Keyboard**: the WAI-ARIA tree model, since virtualisation rules out tabbing through
  every row — one roving tab stop, ↑/↓ move, → opens or descends, ← closes or steps out
  to the parent, Home/End jump, Enter/Space activate the focused row. Focus follows the
  moved row and scrolls it into view; `aria-level`/`posinset`/`setsize` are explicit,
  because the DOM no longer holds the rows a screen reader would otherwise count.

### 6.3 Chat: messages, tool calls, permission prompts
- Column max-width **760px**, centered in panel, 16px side padding, 14px/22px body.
- **User message:** bubble on `--surface-elevated`, `--radius-lg`, padding 8px 12px,
  right-aligned, max-width 85%.
- **Assistant message:** no bubble — full-width text on `--surface-panel` (documents
  read better than chat toys). 8px between blocks; code blocks and tool output on
  `--surface-code` with `--radius-md` + `--border-subtle`, mono 13px — one step
  below the column, not the terminal's black, which would be a 12 L* drop where the
  ramp calls for 6.
- **Tool-call row:** collapsed height 28px, mono 12px `--text-secondary`; 2px left
  border in status color (`--agent-working` pulses via the dot, border steady;
  `--success` / `--error` when settled); chevron expands to output block (instant).
- **Permission prompt:** card on `--surface-elevated`, 1px `--warn`-tinted border
  (`--warn-bg` background wash at header). Buttons 28px height, `--radius-sm`, 13px/500:
  *Allow* = filled `--accent-fill` / `--text-on-accent` (§2.4 — the one action the app
  is blocked on); *Allow always* = outline
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
- Categories are sections, in this order: the window's own commands (uncategorized, no
  header), then **Panes**, **Layouts**, then **Shortcuts**. Uncategorized always leads;
  the registry's categorized rows precede the file's, so a header is always a header and
  never appears mid-list. A capability adds a section by putting a `category` on its
  commands — the QuickBar knows no section names.
- **Pick mode.** A capability may hand the QuickBar a list of its own instead of files
  or commands — the pane picker (§6.11) is the one that does. Same overlay, same rows,
  same keys: only the dialog's accessible name, the placeholder and the rows change, and
  the `>` hint disappears because the prefix has no meaning inside a pick. Sections are
  ranked *within* themselves and never reordered against each other, because in a pick
  the section names are the vocabulary. `Esc` reads **cancel**, not close: the gesture
  that opened it is abandoned. There is no second overlay language in this app.
- **A row a pick cannot honour is shown, not hidden**: `--text-disabled` on both title
  and detail, on a real `disabled` button so the arrows skip it and a screen reader says
  so, and the **detail carries the reason** — the ceiling and the setting that raises it
  ("4 of 4 sessions busy — raise `WORKBENCH_MAX_CONCURRENT_SESSIONS`"). It keeps its
  place in its section: a row that vanishes answers "where did *New agent session* go?"
  with silence, and a row that is offered and then refused spends the whole gesture
  before saying so. Never a dead button (CLAUDE.md, panes).
- Keycap hints: 11px mono on `--surface-elevated`, 1px `--border-default`,
  `--radius-xs`, padding 1px 5px.
- Motion (§5.4): fade + scale `--motion-scale-in`→1 on `--motion-enter`; exit is a
  `--motion-exit` fade, during which the bar is on screen but `pointer-events: none`.
  Row selection is **not** animated — it tracks the arrow key.

### 6.6 Terminal panel
- Bg `--surface-terminal` (deepest surface — the terminal reads as a "well"), no padding
  compromise: 8px inset all sides. Font `--type-term`, ligatures off.
- ANSI palette from §2.7; cursor `--accent`, block, blink off by default; selection
  `--surface-selected`. Scrollbar: 10px overlay, thumb `--border-strong`, transparent
  track.
- Terminal tabs reuse §6.1 at the same 34px, on a `--surface-panel` nested strip
  (§2.1); a live-shell dot is `--success`, steady. **Not** `--agent-working`: it is set
  from "the PTY has not exited", which is true all afternoon, and a standing dot cannot
  wear the colour that means *now* (§2.4).

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
Every binding lives in the command registry; the QuickBar lists the same registry, so
nothing is reachable only by chord and nothing only by mouse. `ui/src/commands.ts` holds
only the window-level commands and assembles the rest — a capability declares its own
commands and their default chords on its tool descriptor (`docs/tools.md`), and
`Ctrl+1..N` is derived from the registered panels in order, not from fixed ids.

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
| `Alt+M` | Toggle focus mode (§6.9) |
| `Alt+S` / `Alt+Shift+S` | Split this pane right / downwards (§6.11) |
| `Alt+←→↑↓` | Focus the pane in that direction |
| `Alt+Shift+←→↑↓` | Swap this pane with the one in that direction |
| `Alt+O` | Focus the next pane |
| `Alt+X` | Close this pane |
| `Alt+A` | Annotate the plan card — point at part of an artifact (§6.3) |

**Pass-through:** inside xterm and Monaco — both full keyboard applications — only
chords carrying `Alt` or `Ctrl+Shift` are intercepted; everything else reaches the
surface (`Ctrl+K` kills a line, `Ctrl+P` walks shell history). Plain keys are never
intercepted anywhere. Hence the Alt twins above: they are the ones that work from
inside a terminal or editor.

**User chords** (`shortcuts.md`, `docs/shortcuts.md`) must carry `Alt`. Everywhere but
xterm and Monaco the app intercepts *any* `Ctrl` chord and preventDefaults it, so a
file-supplied `Ctrl+V` would take paste away from the chat box, the rename field and the
QuickBar's own input. Alt is both the safe set and the one that reaches every surface.

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

### 6.9 Focus mode and the layout chip

The window remembers its arrangement, and one panel can take the whole of it. Both are
one capability (`ui/src/panels/Layouts.tsx`) and both are stated in one place: the
**layout chip**, at the right end of the status bar.

- **Focus mode** is a *keyboard* affordance: `Alt+M` fills the window with the focused
  panel, `Alt+M` gives the arrangement back. The tab strip gains nothing — panel tabs
  stay chrome (§6.1), and a maximize button on every tab would spend permanent pixels on
  an occasional action. What the user gets instead is unmistakable: every other panel is
  gone, and the chip says so.
- **Chip:** the session-chip anatomy of §6.4 — 18px pill, `--radius-full`, 11px label —
  with a 12px outline glyph before it, max 140px, truncating. Its label is the layout you
  are in (`Review`, a name you saved), or `Layout` when it is nobody's in particular.
- **In focus mode** the chip reads `Focused` and fills: `--accent-muted` background, 1px
  `--accent` border, `--text-primary` label. This is the one place chrome states a
  *state of the window* rather than of a document, and the only status item that ever
  carries accent — spend it here and nowhere else in the bar (§1.3). Its tooltip names
  the way out (`Alt+M`), because a filled window has no other visible exit.
- **Layout menu:** clicking the chip opens a 260px popover above the bar —
  `--surface-overlay`, 1px `--border-default`, `--radius-md`, `--shadow-2`, the menu
  language of §4 at elevation 2. `position: fixed`, because the status bar clips its
  overflow. Contents, top to bottom: an 11px uppercase `Layouts` label, one 28px row per
  built-in layout, a `Saved` label and one row per saved layout with a 24px `×` that
  turns `--error` on hover, and a hairline-separated footer holding a 26px name field and
  one outline **Save** button.
- The chip is a mouse path to everything the QuickBar's **Layouts** section already
  reaches (§6.5), and `shortcuts.md` can bind any layout by name
  (`docs/shortcuts.md`). Nothing here is reachable only one way.
- A layout that could not be restored — a stale entry, a corrupt file — resolves to the
  default arrangement plus **one** warning toast (§6 toasts). Never a blank window, and
  never a modal: losing an arrangement is not worth interrupting for.

### 6.10 Empty states
- Centered, max-width 260px. Icon 32px, 1.5px stroke, `--text-tertiary`. Title 14px/600
  `--text-secondary`; hint 12px `--text-tertiary`; optional single action as `--accent`
  link or one outline button — never a filled button in an empty state.
- Always name the shortcut: "Open a file — Ctrl+P" with keycap styling (§6.5).
- Document panels' empty/loading states render on paper colors (§2.8), not panel colors.

### 6.11 Panes: splitting, focus, and what goes in one

Workbench is a tiling window manager that happens to host an editor. A **pane** is one
dockview group; any pane splits in two, anything registered goes in the new one, and the
arrangement belongs to the user. The whole system is one capability
(`ui/src/panels/Panes.tsx`) and it adds exactly three surfaces.

- **The split affordance.** Two 12px outline glyphs — a square divided vertically, a
  square divided horizontally — in 20px hit areas at the right end of **the focused
  pane's** tab strip, `--text-tertiary`, `--surface-hover` + `--text-primary` on hover.
  On the focused pane only: chrome recedes (§1.1), and a control on all six tab strips in
  a full window is five controls nobody is looking at — where the keyboard is, is where
  the mouse path belongs. Panel tabs themselves gain nothing (§6.1 stands).
- **Pane focus** is the live rule: the focused group's tab strip carries the 2px
  full-bleed `--accent` rule (§6.1). Splitting, swapping and directional
  movement all leave that mark on the pane you ended up in — it is the only feedback the
  keyboard commands produce, and it must never be ambiguous, so **exactly one** pane
  carries it.
- **The focused pane is the one you are talking to.** With several agent panes open,
  the session that `Enter`, *Interrupt* and a `prompt` shortcut mean is the one in the
  focused pane; likewise `Ctrl+S` saves the file in the focused editor pane and a `shell`
  shortcut types into the focused terminal pane. This is the tmux rule, and it is why no
  pane needs a "make me active" control of its own.
- **The picker** is the QuickBar in pick mode (§6.5), never a second overlay: the same
  640px surface, the same 40px rows, the same keycaps and the same `↑↓ / Enter / Esc`
  footer. It is titled by the gesture ("Split this pane to the right") and its sections
  are the vocabulary of what a pane can hold — **Panels** (every registered tool),
  **Agent** / **Agent sessions**, **Terminal**, **Open files**.
- **One pane per identity.** Picking something that already has a pane *moves* that pane
  into the split rather than cloning it. Two panes of the same conversation, the same
  file or the same shell would be two views claiming one thing, and a saved arrangement
  could not restore either faithfully.
- **A pane entering** is the one panel-level movement in the app: opacity 0→1 with a
  0.985→1 scale over `--duration-2` `--ease-standard` (§5, opacity and transform only).
  Nothing else about a split animates — the grid resize is 1:1 with the pointer as it
  always was, and `prefers-reduced-motion` already flattens the duration globally.
- **A pane whose binding no longer resolves** — a session that stopped, a document that
  must open in the Editor pane — says so in a one-line bar with the anatomy of §6.1's
  conflict and provenance bars (12px, `--warn-bg`, bottom hairline, one 24px outline
  button on the right). Never an empty pane, never a modal.
- **The floor is one pane.** Closing the last one is refused with a warning toast: a dock
  with nothing in it has no tab to click and no pane to split, and "Switch to the Default
  layout" (§6.9) is how the rest comes back.
- **The default arrangement is unchanged.** A new workspace still opens Files / Editor /
  Agent / Terminal in the same four places; nobody has to build a layout to start.

---

## 7. Accessibility

- **Contrast**: body text ≥ 4.5:1 on its surface; large text (≥18.66px/600) and UI
  glyphs ≥ 3:1. The pairs in §2 are *measured*, and the measurement is a gate:
  `ui/e2e/palette.test.ts` re-derives every figure below from `tokens.css` and fails the
  build on drift. Verify any new pair there, not by eye.

  | Pair | Dark | Light | Floor |
  |---|---|---|---|
  | `--text-primary` on `--surface-panel` | 14.94:1 | 14.90:1 | 4.5 |
  | `--text-secondary` on `--surface-panel` | 11.21:1 | 11.24:1 | 4.5 |
  | `--text-tertiary` on `--surface-panel` | 7.86:1 | 7.83:1 | 4.5 |
  | `--text-tertiary` on `--surface-app` (status bar, 11px) | 5.57:1 | 6.28:1 | 4.5 |
  | `--text-tertiary` on `--surface-overlay` (QuickBar, 11px) | **4.55:1** | 5.61:1 | 4.5 |
  | `--text-primary` on `--surface-terminal` | 19.26:1 | 18.26:1 | 4.5 |
  | `--text-on-accent` on `--accent-fill` | 11.04:1 | 11.04:1 | 4.5 |
  | `--accent` as text on `--surface-panel` | 9.76:1 | 4.67:1 | 4.5 |
  | `--accent` as the live rule on `--surface-app` | 6.92:1 | 3.75:1 | 3.0 |
  | `--border-subtle` on `--surface-panel` | 2.51:1 | 2.51:1 | — (§1.4) |
  | every `--ansi-*` on `--surface-code` (bar the dim `black`) | ≥ 5.34:1 | ≥ 5.42:1 | 4.5 |

  Every text pair in the system is ≥ 5:1 except one, named here rather than rounded
  away: `--text-tertiary` on `--surface-overlay` in dark, at 4.55:1 — above the floor,
  and the test pins it so it cannot quietly become the second. `--text-disabled` (4.42:1)
  is exempt under WCAG 1.4.3; a disabled row that reads as enabled is the worse failure.
  `--accent` is not text-capable on light chrome and is never used as text there — §2.8
  says why, and it is the reason `--accent-fill` exists.
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
