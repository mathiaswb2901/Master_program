/**
 * The editor wears the design system — in the running window, on two panes at
 * once, and through a live theme switch.
 *
 * `e2e/theme.spec.ts` already owns the *race*: a toggle that lands while the
 * Monaco chunk is still on the wire. This journey owns the steady state that
 * V7a is about, and asserts the three things a screenshot would be trusted for
 * and a unit test cannot reach:
 *
 *  1. **The buffer is the well, not a vendor grey.** `--surface-code`, read out
 *     of the running document rather than written here, so the assertion holds
 *     in both themes and survives a palette retune.
 *  2. **The syntax palette is the terminal's.** A markdown header is Monaco's
 *     `keyword`, and the theme maps `keyword` to `--ansi-magenta` — the same
 *     magenta xterm paints beside it (DESIGN.md §2.7). This is the assertion
 *     that would have failed before V7a: `inherit: true` left every unnamed role
 *     on VS Code's colours, and the rules that *were* named were a third of the
 *     ones the shipped grammars emit.
 *  3. **Two panes, one theme.** Monaco's theme lives on a global service, so
 *     "it works with one editor" is not evidence for the window a user actually
 *     arranges (CLAUDE.md, panes). The journey splits a second editor pane onto
 *     the same file and holds *both* to every colour, before and after the flip.
 *
 * Every expected colour is resolved from `tokens.css` **inside the page**, at
 * the moment of the assertion. A hard-coded `rgb(198, 139, 245)` here would be
 * the parallel palette this whole change exists to refuse, one directory over.
 */

import { expect, request, test, type Page } from "@playwright/test";

import { openApp, treeItem } from "./app";
import { NOTES_FILE } from "./workspace";

/** Leave the window as the journeys after this one expect to find it. */
test.afterAll(async () => {
  const context = await request.newContext({ baseURL: test.info().project.use.baseURL });
  await context.put("/api/layouts", { data: { current: null, current_name: null, saved: [] } });
  await context.dispose();
});

/**
 * A token's value as the browser's own `rgb(…)` string — the exact form a
 * computed style reads back in, so the two sides of every assertion below are
 * comparable without any colour maths in the harness.
 */
function token(page: Page, name: string): Promise<string> {
  return page.evaluate((property) => {
    const probe = document.createElement("span");
    probe.style.color = `var(${property})`;
    document.body.append(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    return value;
  }, name);
}

/**
 * Slots the buffer cannot show you, checked where Monaco actually publishes
 * them: `defineTheme`'s colours land on `.monaco-editor` as `--vscode-<id>`
 * custom properties, so the find widget, the minimap slider and the bracket
 * pairs are all readable without opening any of them. Each pairs with the design
 * token it is supposed to be.
 *
 * `editorBracketHighlight.foreground1` is the one to read first, and it is the
 * one this journey was written around: with the slot unset Monaco falls back to
 * its own default, which in dark is `#FFD700` — a gold ΔE 10 from `--accent`, on
 * every opening brace in the file, wearing the one colour that is supposed to
 * mean *here* and *now*. (Deleting the six bracket entries from the theme and
 * re-running this spec is how that was confirmed: light falls back to `#0431FA`,
 * and the pair below reports it.)
 */
const CHROME: readonly (readonly [slot: string, token: string])[] = [
  ["--vscode-editor-background", "--surface-code"],
  ["--vscode-editorGutter-background", "--surface-code"],
  ["--vscode-editorLineNumber-foreground", "--text-tertiary"],
  ["--vscode-editorCursor-foreground", "--accent"],
  ["--vscode-editorBracketHighlight-foreground1", "--ansi-white"],
  ["--vscode-editorBracketHighlight-foreground2", "--ansi-blue"],
  ["--vscode-editor-findMatchBackground", "--accent-muted"],
  ["--vscode-editorStickyScroll-background", "--surface-panel"],
  ["--vscode-editorSuggestWidget-background", "--surface-overlay"],
  ["--vscode-editorWidget-border", "--border-default"],
  ["--vscode-minimap-background", "--surface-code"],
  ["--vscode-progressBar-background", "--accent"],
];

/** Each `[slot, token]` pair as two comparable colour strings, resolved in the
 * page so `#f3f3f3` and `var(--surface-code)` can be compared at all. */
function chrome(page: Page): Promise<Record<string, [string, string]>> {
  return page.evaluate((pairs) => {
    const editor = document.querySelector(".monaco-editor");
    if (editor === null) return {};
    const style = getComputedStyle(editor);
    const paint = (value: string): string => {
      const probe = document.createElement("span");
      probe.style.color = value;
      document.body.append(probe);
      const painted = getComputedStyle(probe).color;
      probe.remove();
      return painted;
    };
    const out: Record<string, [string, string]> = {};
    for (const [slot, token] of pairs) {
      out[slot] = [paint(style.getPropertyValue(slot).trim()), paint(`var(${token})`)];
    }
    return out;
  }, CHROME);
}

/** Every editor in the window: its buffer background, and the colours its
 * highlighted spans are painted in. One entry per Monaco instance. */
function editors(page: Page): Promise<{ background: string; syntax: string[] }[]> {
  return page.evaluate(() =>
    [...document.querySelectorAll(".monaco-editor")].map((editor) => {
      const background = editor.querySelector(".monaco-editor-background");
      return {
        background:
          background === null ? "" : getComputedStyle(background).backgroundColor,
        syntax: [
          ...new Set(
            [...editor.querySelectorAll(".view-lines span[class*=mtk]")].map(
              (span) => getComputedStyle(span).color,
            ),
          ),
        ],
      };
    }),
  );
}

/** Wait until every editor on screen has painted, and report what they painted. */
async function paintedEditors(
  page: Page,
  count: number,
): Promise<{ background: string; syntax: string[] }[]> {
  await expect
    .poll(async () => {
      const found = await editors(page);
      return found.length === count && found.every((one) => one.syntax.length > 0);
    })
    .toBe(true);
  return editors(page);
}

test("the buffer, its syntax and every pane of it follow the design tokens", async ({ page }) => {
  await openApp(page);

  await test.step("open a file, and a second pane onto it", async () => {
    await treeItem(page, NOTES_FILE).click();
    await expect(page.locator(".wb-editor-body .monaco-editor").first()).toBeVisible();

    // The pane picker's own gesture (§6.11): split, then choose the open file.
    // Two panes on one file is the cheapest *real* second instance — same model,
    // two editors — and it is the shape the theming has to survive, because the
    // theme is global to Monaco and the editors are not.
    await page.keyboard.press("Alt+S");
    const picker = page.getByRole("dialog", { name: "Split this pane to the right" });
    await expect(picker).toBeVisible();
    await picker.locator(".wb-qb-row", { hasText: NOTES_FILE }).first().click();
    await expect(picker).toBeHidden();
  });

  await test.step("both buffers are the code well, and both speak ANSI", async () => {
    const painted = await paintedEditors(page, 2);
    const code = await token(page, "--surface-code");
    const magenta = await token(page, "--ansi-magenta");
    expect(code, "the token resolved to a real colour").toMatch(/^rgba?\(/);

    for (const [index, one] of painted.entries()) {
      expect(one.background, `editor ${String(index)} background`).toBe(code);
      // `# Notes` — a markdown header, which Monaco tokenises as `keyword`.
      expect(one.syntax, `editor ${String(index)} syntax palette`).toContain(magenta);
    }
  });

  await test.step("so is the chrome no buffer can show you", async () => {
    for (const [slot, [painted, wanted]] of Object.entries(await chrome(page))) {
      expect(wanted, `${slot}: the token resolved to a real colour`).toMatch(/^rgba?\(/);
      expect(painted, slot).toBe(wanted);
    }
  });

  await test.step("a live theme switch turns both of them over", async () => {
    const before = await token(page, "--surface-code");

    await page.keyboard.press("Control+Shift+P");
    const quickbar = page.getByRole("dialog", { name: "Quick open" });
    await quickbar.locator(".wb-qb-input").fill(">Toggle theme");
    await quickbar.locator(".wb-qb-row", { hasText: "Toggle theme" }).first().click();
    await expect(quickbar).toBeHidden();
    await expect.poll(() => token(page, "--surface-code")).not.toBe(before);

    // Not "it changed" — it changed *to the other theme's tokens*, chrome and
    // syntax together. The failure this catches is a theme rebuilt from the
    // palette that was on screen a moment ago, which looks like a working
    // toggle until you read the numbers.
    const code = await token(page, "--surface-code");
    const magenta = await token(page, "--ansi-magenta");
    await expect
      .poll(async () => (await editors(page)).map((one) => one.background))
      .toEqual([code, code]);
    for (const [index, one] of (await paintedEditors(page, 2)).entries()) {
      expect(one.syntax, `editor ${String(index)} after the flip`).toContain(magenta);
    }
    // And the chrome with it — a theme is rebuilt from the tokens on screen, so
    // every slot has to have moved, not only the ones that are visible.
    for (const [slot, [painted, wanted]] of Object.entries(await chrome(page))) {
      expect(painted, `${slot} after the flip`).toBe(wanted);
    }
  });

  await test.step("and the caret is the one amber the buffer is allowed", async () => {
    // DESIGN.md §2.4: *where I am*. The cursor is the editor's whole share of
    // the accent — the line number beside it is deliberately not amber, because
    // two marks for one fact is redundancy rather than information.
    await page.locator(".wb-editor-body .monaco-editor").first().click();
    const accent = await token(page, "--accent");
    await expect
      .poll(() =>
        page.evaluate(() => {
          const cursor = document.querySelector(".monaco-editor .cursor");
          return cursor === null ? "" : getComputedStyle(cursor).backgroundColor;
        }),
      )
      .toBe(accent);

    const numbers = await page.evaluate(() => {
      const line = document.querySelector(".monaco-editor .line-numbers");
      return line === null ? "" : getComputedStyle(line).color;
    });
    expect(numbers, "line numbers are metadata, not a mark").not.toBe(accent);
    expect(numbers).toBe(await token(page, "--text-tertiary"));
  });
});
