/**
 * The "New document…" affordance (M5 item 16).
 *
 * The missing thing was never the format — it was the affordance: a user should
 * not have to know that a spreadsheet is spelled `.xlsx`. So this offers document
 * kinds **by name** (Word / Excel / PowerPoint / Python / Text / Markdown /
 * Notebook), and the wire carries the extension. Two surfaces use it — the file
 * tree's context menu and toolbar, and the QuickBar command — and both go through
 * `newDocumentFlow`, so the vocabulary lives in exactly one place.
 *
 * The flow is a two-step pick reusing the QuickBar (`store.ts`'s `QuickPick`),
 * never a second overlay: choose a kind, then name it. The name step follows the
 * workspace picker's pattern — a row that exists only because it was typed — so
 * the server's atomic, jailed create does the rest.
 */

import { joinPath } from "./filetree";
import { type QuickPick, useStore } from "./store";
import type { DocumentKind } from "./types";

export interface DocumentKindInfo {
  kind: DocumentKind;
  /** The name the user picks by (DESIGN.md: offer kinds by name, not extension). */
  name: string;
}

/**
 * The kinds, in the order the picker lists them: the three Office documents that
 * need real content first, then the plain-text kinds, then the notebook. The
 * name is the noun a user reaches for; the extension is `kind` itself.
 */
export const DOCUMENT_KINDS: readonly DocumentKindInfo[] = [
  { kind: "docx", name: "Word" },
  { kind: "xlsx", name: "Excel" },
  { kind: "pptx", name: "PowerPoint" },
  { kind: "py", name: "Python" },
  { kind: "txt", name: "Text" },
  { kind: "md", name: "Markdown" },
  { kind: "ipynb", name: "Notebook" },
];

/**
 * The workspace-relative path a document gets, given a folder and a typed name.
 *
 * The extension is appended unless the user already typed it, so "budget" and
 * "budget.xlsx" both land on `budget.xlsx` — and a name with slashes creates the
 * folders on the way, because the server's write does (`mkdir(parents=True)`).
 */
export function documentPath(parent: string, name: string, kind: DocumentKind): string {
  const trimmed = name.trim();
  const file = trimmed.toLowerCase().endsWith(`.${kind}`) ? trimmed : `${trimmed}.${kind}`;
  return joinPath(parent, file);
}

/**
 * Open a QuickPick *after* the current one's own `close()` has run.
 *
 * A QuickBar row runs its action and then closes the palette synchronously
 * (`QuickBar.tsx`), and closing clears any pick. Opening the next step inline
 * would therefore be wiped a microtask later; deferring past this turn lets the
 * new pick survive. Harmless when there is no open palette (the tree menu path):
 * it is one turn's delay before the picker appears.
 */
function laterQuickPick(pick: QuickPick): void {
  // Plain `setTimeout`, not `window.setTimeout`: the global one, which is what
  // both the browser and the unit-test's fake timers hand back.
  setTimeout(() => useStore.getState().openQuickPick(pick), 0);
}

/** Step 1: choose a kind by name. */
export function newDocumentFlow(parent: string): void {
  laterQuickPick({
    label: "New document",
    placeholder: "What kind of document?",
    rows: () =>
      DOCUMENT_KINDS.map((info) => ({
        key: info.kind,
        title: info.name,
        detail: `.${info.kind}`,
        category: "New document",
        run: () => pickName(parent, info),
      })),
  });
}

/** Step 2: name it. The single row is created verbatim from what was typed, so
 * the QuickBar's own fuzzy filter always keeps it (the title contains the name). */
function pickName(parent: string, info: DocumentKindInfo): void {
  laterQuickPick({
    label: `New ${info.name} document`,
    placeholder: `Name for the new ${info.name} document`,
    rows: (query) => {
      const name = query.trim();
      if (name === "") {
        return [
          {
            key: "hint",
            title: `Type a name for the new ${info.name} document`,
            detail: `.${info.kind} is added for you`,
            disabled: true,
            run: () => undefined,
          },
        ];
      }
      const path = documentPath(parent, name, info.kind);
      return [
        {
          key: "create",
          title: `Create ${path}`,
          detail: parent === "" ? "in the workspace root" : `in ${parent}`,
          run: () => void useStore.getState().createDocument(path, info.kind),
        },
      ];
    },
  });
}
