import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// Only global used here; @types/node would be a dependency for one line.
declare const process: { env: Record<string, string | undefined> };

/** Backend the dev server and `vite preview` proxy to.
 *
 * `WORKBENCH_SERVER_URL` wins — the Playwright suite runs its own backend on
 * another port and sets it (playwright.config.ts). Without it we fall back to
 * the server's *own* settings, `WORKBENCH_HOST`/`WORKBENCH_PORT`, which are also
 * what the desktop shell probes and spawns with (`desktop/src-tauri/src/backend.rs`).
 * One authority for the pair is the whole point: `WORKBENCH_PORT=9000 npm run
 * tauri dev` used to give you a live backend on 9000 and a page proxying every
 * REST call and WebSocket to 8787 — a dead UI under a log line saying the
 * backend was ready.
 */
const host = process.env.WORKBENCH_HOST || "127.0.0.1";
const port = process.env.WORKBENCH_PORT || "8787";
// A server bound to the wildcard is still reached over loopback from here.
const loopback = host === "0.0.0.0" || host === "::" ? "127.0.0.1" : host;
const backend = process.env.WORKBENCH_SERVER_URL ?? `http://${loopback}:${port}`;

const proxy = {
  "/api": { target: backend },
  "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
};

/**
 * Where the bundle report lands. Emitted into `dist` as an ordinary asset —
 * rollup can do that with no filesystem access, so this plugin stays inside the
 * type-checked config with no `@types/node` and no path handling of its own. It
 * is ~4 kB of aggregated numbers; the alternative (a per-module dump) would be
 * a third of a megabyte of absolute paths shipped in the app.
 */
const METAFILE = "bundle-metafile.json";

/** Package (or first-party directory) a rollup module id is attributed to. */
function attributionOf(id: string, root: string): string {
  // Rollup marks virtual modules with a NUL; Vite appends query suffixes
  // (`?worker`, `?used`, `?commonjs-proxy`) that are not part of the path.
  const clean = id.replace(/^\0/, "").replace(/\\/g, "/").split("?")[0];
  const nodeModules = clean.lastIndexOf("/node_modules/");
  if (nodeModules >= 0) {
    const rest = clean.slice(nodeModules + "/node_modules/".length).split("/");
    const scoped = rest[0].startsWith("@") ? rest.slice(0, 2) : rest.slice(0, 1);
    return `node_modules/${scoped.join("/")}`;
  }
  if (clean.startsWith(root + "/")) return clean.slice(root.length + 1).split("/")[0];
  // Vite's own injected helpers (preload, HMR shims) and anything else without
  // a path we can place. Named rather than dropped: an unattributed megabyte
  // must be visible in the report, not rounded away.
  return "virtual";
}

interface Attribution {
  bytes: number;
  modules: number;
}

/**
 * Write rollup's own module attribution next to the bundle, so a bundle budget
 * can assert *what is in a chunk* rather than eyeball what a file weighs.
 *
 * The question this exists to answer is `ui/e2e/perf/bundle.spec.ts`'s: does any
 * module under `node_modules/monaco-editor` appear in the eager entry chunk?
 * A file size cannot answer it — a 500 kB entry chunk with a corner of Monaco
 * in it is still an entry chunk that has to parse Monaco before the app starts.
 */
function bundleMetafile(): Plugin {
  let root = "";
  return {
    name: "workbench:bundle-metafile",
    apply: "build",
    configResolved(config) {
      root = config.root.replace(/\\/g, "/").replace(/\/$/, "");
    },
    generateBundle(_options, bundle) {
      const outputs = Object.values(bundle)
        .filter((output) => output.type === "chunk")
        .map((chunk) => {
          const totals = new Map<string, Attribution>();
          for (const [id, module] of Object.entries(chunk.modules)) {
            const key = attributionOf(id, root);
            const running = totals.get(key) ?? { bytes: 0, modules: 0 };
            running.bytes += Math.max(module.renderedLength, 0);
            running.modules += 1;
            totals.set(key, running);
          }
          const attribution: Record<string, Attribution> = {};
          for (const [key, value] of [...totals].sort((a, b) => b[1].bytes - a[1].bytes)) {
            attribution[key] = value;
          }
          return {
            fileName: chunk.fileName,
            isEntry: chunk.isEntry,
            isDynamicEntry: chunk.isDynamicEntry,
            // Characters of generated code. The authoritative byte and gzip
            // numbers come from the emitted file; this is here so the report
            // reads on its own.
            codeLength: chunk.code.length,
            moduleCount: Object.keys(chunk.modules).length,
            attribution,
          };
        })
        .sort((a, b) => Number(b.isEntry) - Number(a.isEntry) || b.codeLength - a.codeLength);
      this.emitFile({
        type: "asset",
        fileName: METAFILE,
        source: JSON.stringify({ outputs }, null, 2),
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), bundleMetafile()],
  // `index.html` is the app and the *only* Rollup entry — a second one splits the
  // launch cost across two chunks and fails the bundle budget (`e2e/perf/
  // bundle.spec.ts`, which asserts exactly one entry chunk). `popout.html` is the
  // blank same-origin page dockview opens with `window.open('/popout.html')` when
  // a pane is popped out to its own window (M5 item 13, `ui/src/panels/Panes.tsx`).
  // It carries no module script — only an inline theme guard — so it needs no
  // bundling: it lives in `public/` and Vite copies it verbatim to `dist/
  // popout.html`, served at `/popout.html` in dev, `vite preview`, and the build.
  server: { proxy },
  // `vite preview` serves the built bundle; it needs the same proxy so the E2E
  // suite (and anyone eyeballing a production build) talks to a live backend.
  preview: { proxy },
});
