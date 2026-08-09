/**
 * Budget — what is in the eager entry chunk, and what it weighs.
 *
 * The other perf specs measure the app running. This one measures the app
 * *arriving*, and it is the only budget in the lane that needs no browser: a
 * module script blocks `DOMContentLoaded` until it has been downloaded, parsed
 * and evaluated, so every byte in the entry chunk is paid before the first
 * pixel, on every cold start, forever.
 *
 * Two assertions, and the second is the one that will still be earning its keep
 * in a year:
 *
 *  1. **A gzipped ceiling on the entry chunk.** The number a cold start pays.
 *  2. **No module under `node_modules/monaco-editor` in it.** A size ceiling
 *     alone does not hold this line — Monaco can creep back in a corner at a
 *     time, each import individually defensible, and the ceiling only fails
 *     once it is far too late. Presence is the invariant; the size is the
 *     symptom. It is asserted from rollup's own module attribution
 *     (`dist/bundle-metafile.json`, written by the plugin in `vite.config.ts`),
 *     because a file listing cannot tell you what is *inside* a chunk.
 *
 * Deterministic — same bytes on a laptop and on a throttled runner — so it
 * carries no `@wallclock` tag and blocks a merge like the other count budgets.
 */

import { gzipSync } from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect } from "@playwright/test";

// The lane's `test`, like every spec here — even though these three read the
// build off disk and never open a page, which is exactly why it costs them
// nothing: a fixture no test requests is never set up (`./window`).
import { test } from "./window";

/** `ui/dist`, from `<ui>/e2e/perf/`. Built by `npm run perf` before this runs. */
const DIST = path.resolve(fileURLToPath(new URL(".", import.meta.url)), "..", "..", "dist");
const METAFILE = path.join(DIST, "bundle-metafile.json");

/**
 * Measured on the author's machine, production build, 2026-08-05:
 *
 * | | entry chunk | gzipped | Monaco modules in it |
 * |---|---|---|---|
 * | before | 3,999.4 KiB | 1,030.2 KiB | 961 |
 * | after  |   725.4 KiB |   190.9 KiB | 0 |
 *
 * Monaco was 87.8% of the entry chunk's attributed bytes, reached through one
 * barrel import (`import * as monaco from "monaco-editor"`) that `main.tsx`
 * pulled in statically. It now arrives on its own dynamic chunk, so none of it
 * is on the launch path. (The eager *stylesheet* went the same way as a side
 * effect: 225 KiB to 92 KiB, with Monaco's 133 KiB following its chunk.)
 *
 * Both rows are the same commit with and without the change, so the difference
 * is this change's. Rebased onto master an hour later — with the terminal and
 * visual-artifacts lanes underneath — the entry chunk reads **738.9 KiB raw,
 * 195.4 KiB gzipped, still 0 Monaco modules**, which is the number the ceiling
 * below has to live with.
 *
 * What is left is dockview-core, xterm (285 KiB), our own `src` and react-dom
 * (132 KiB) — the next things anyone shrinking this number has to argue with.
 *
 * ## Raised for dockview 7, 2026-08-09
 *
 * | | entry chunk | gzipped | dockview attributed |
 * |---|---|---|---|
 * | dockview 4.13.1 | 879.8 KiB | 236.5 KiB | 451 KiB over 80 modules |
 * | dockview 7.0.4  | 1032.2 KiB | 271.9 KiB | 666 KiB over 4 modules |
 *
 * **+35.4 KiB gzipped, +15.0%**, and it is packaging rather than features. v4
 * resolved to per-module ESM, so rollup could see 66 dockview-core modules and
 * shake the ones we never reach; 7 publishes a pre-bundled `main.esm.mjs` and
 * its `exports` map offers no other entry, so the whole library arrives as one
 * opaque module. The `dockview` package additionally calls `registerModules()`
 * at import — accessibility, advanced drag-and-drop, tab groups, context menus
 * — so even reaching past the export map would not shake much. Measured, not
 * assumed: aliasing back to `dist/esm` was tried and abandoned (it fights the
 * package's own export map for a gain the eager `registerModules` call cancels).
 *
 * The dock is the app shell — nothing paints without it — so this cannot move
 * behind a dynamic import the way Monaco did. The ceiling therefore goes to 285
 * KiB: 13 KiB (4.8%) of headroom, deliberately tighter than the 1.33x this
 * number started at, because the thing that grew is not going to grow again on
 * its own and the next PR to touch it should have to argue. Lower it when a PR
 * earns it.
 */
const ENTRY_GZIP_CEILING_KB = 285;

/** Rollup's per-chunk module attribution, aggregated by package. */
interface Metafile {
  outputs: {
    fileName: string;
    isEntry: boolean;
    isDynamicEntry: boolean;
    codeLength: number;
    moduleCount: number;
    attribution: Record<string, { bytes: number; modules: number }>;
  }[];
}

function readMetafile(): Metafile {
  expect(
    fs.existsSync(METAFILE),
    `no bundle metafile at ${METAFILE} — run \`npm run build\` first`,
  ).toBe(true);
  return JSON.parse(fs.readFileSync(METAFILE, "utf-8")) as Metafile;
}

function entryChunk(metafile: Metafile): Metafile["outputs"][number] {
  const entries = metafile.outputs.filter((output) => output.isEntry);
  // One entry, always: a second would mean the launch cost is split across two
  // files and neither number below describes it.
  expect(entries.map((entry) => entry.fileName)).toHaveLength(1);
  return entries[0];
}

test("the eager entry chunk stays under its gzipped ceiling", async () => {
  // No fixture, no page: `test.info()` rather than the second callback
  // argument, which would need a fixtures object this test does not use.
  const info = test.info();
  const entry = entryChunk(readMetafile());
  const bytes = fs.readFileSync(path.join(DIST, entry.fileName));
  const gzipKb = gzipSync(bytes, { level: 9 }).byteLength / 1024;
  const rawKb = bytes.byteLength / 1024;

  const top = Object.entries(entry.attribution)
    .slice(0, 5)
    .map(([name, share]) => `${name} ${(share.bytes / 1024).toFixed(0)} kB`)
    .join(", ");
  info.annotations.push({
    type: "perf",
    description:
      `entry-chunk: ${entry.fileName} — ${rawKb.toFixed(1)} kB raw, ` +
      `${gzipKb.toFixed(1)} kB gzipped, ${String(entry.moduleCount)} modules (${top})`,
  });
  await info.attach("entry-chunk.json", {
    body: JSON.stringify({ rawKb, gzipKb, ...entry }, null, 2),
    contentType: "application/json",
  });

  expect(gzipKb).toBeLessThan(ENTRY_GZIP_CEILING_KB);
});

test("no Monaco module is in the eager entry chunk", () => {
  const entry = entryChunk(readMetafile());
  const monaco = Object.entries(entry.attribution).filter(([name]) =>
    name.startsWith("node_modules/monaco-editor"),
  );
  expect(
    monaco.map(([name, share]) => `${name}: ${String(share.modules)} modules`),
    "Monaco is back on the launch path — it must be reached through a dynamic " +
      "import (see ui/src/monaco.ts), never a static one",
  ).toEqual([]);
});

test("the editor loads as its own chunk", () => {
  // The other half of the invariant above: absent from the entry chunk is only
  // good news if it is present somewhere reachable. A refactor that dropped the
  // editor entirely would pass the two budgets above and fail every user.
  const metafile = readMetafile();
  const carrying = metafile.outputs.filter((output) =>
    Object.keys(output.attribution).some((name) => name.startsWith("node_modules/monaco-editor")),
  );
  expect(carrying.length, "no chunk contains Monaco at all").toBeGreaterThan(0);
  expect(
    carrying.every((chunk) => chunk.isDynamicEntry || !chunk.isEntry),
    "Monaco must arrive through a dynamic import",
  ).toBe(true);
});
