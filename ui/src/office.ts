/**
 * OnlyOffice DocsAPI boundary: office-file detection, the api.js script loader
 * (module-level promise — injected once per app lifetime), and a minimal
 * ambient typing for the `DocsAPI` global so no `any` leaks past this file.
 */

import type { DocEditorConfig } from "./types";

/** Extensions that open in an Office tab (.csv stays in Monaco). */
const OFFICE_EXTENSIONS = new Set([
  "docx",
  "doc",
  "odt",
  "rtf",
  "xlsx",
  "xls",
  "ods",
  "pptx",
  "ppt",
  "odp",
]);

export function isOfficePath(path: string): boolean {
  const name = path.split("/").pop() ?? "";
  const dot = name.lastIndexOf(".");
  return dot > 0 && OFFICE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

/** The backend config plus the sizing the panel always applies. */
export type DocsApiEditorOptions = DocEditorConfig & { height: string; width: string };

export interface DocEditorInstance {
  destroyEditor(): void;
}

interface DocsApiGlobal {
  DocEditor: new (elementId: string, config: DocsApiEditorOptions) => DocEditorInstance;
}

declare global {
  interface Window {
    DocsAPI?: DocsApiGlobal;
  }
}

let docsApiPromise: Promise<DocsApiGlobal> | null = null;

/** Inject `{server_url}/web-apps/apps/api/documents/api.js` once and resolve
 * with the DocsAPI global. A failed load clears the cache so a later open
 * retries (e.g. the Document Server came up in the meantime). */
export function loadDocsApi(serverUrl: string): Promise<DocsApiGlobal> {
  if (docsApiPromise !== null) return docsApiPromise;
  docsApiPromise = new Promise<DocsApiGlobal>((resolve, reject) => {
    if (window.DocsAPI) {
      resolve(window.DocsAPI);
      return;
    }
    const script = document.createElement("script");
    script.src = `${serverUrl.replace(/\/+$/, "")}/web-apps/apps/api/documents/api.js`;
    script.async = true;
    script.onload = () => {
      if (window.DocsAPI) resolve(window.DocsAPI);
      else reject(new Error("Document Server script loaded but DocsAPI is missing"));
    };
    script.onerror = () => {
      docsApiPromise = null;
      script.remove();
      reject(new Error("Could not load the Document Server script"));
    };
    document.head.appendChild(script);
  });
  return docsApiPromise;
}

let preconnectDone = false;

/** Warm up the Document Server before the first office tab opens: a
 * `<link rel="preconnect">` to its origin plus a background api.js load.
 * Failures are silent — `loadDocsApi` clears its cache on error, so the
 * first real open retries and surfaces the problem to the user. */
export function preloadDocsApi(serverUrl: string): void {
  if (!preconnectDone) {
    try {
      const link = document.createElement("link");
      link.rel = "preconnect";
      link.href = new URL(serverUrl).origin;
      document.head.appendChild(link);
      preconnectDone = true;
    } catch {
      // malformed server_url — the script load below reports it on first open
    }
  }
  void loadDocsApi(serverUrl).catch(() => {
    // Document Server unreachable right now — retried on first office open.
  });
}
