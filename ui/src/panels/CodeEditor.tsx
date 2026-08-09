/**
 * The Monaco surface, in its own module so it can be `React.lazy`-loaded.
 *
 * Everything else in the editor panel — the tab strip, the empty state, the
 * provenance and conflict bars — is cheap and must be on screen at once. This
 * is the part that costs 3.5 MB, and it is the part nobody needs until they
 * open a text file. `EditorArea.tsx` mounts it behind a `<Suspense>`; by then
 * the idle-time prefetch has usually already fetched the chunk.
 *
 * `@monaco-editor/react` is imported *here* rather than in the panel for the
 * same reason: it drags `@monaco-editor/loader` with it, and the loader's whole
 * job is fetching Monaco from a CDN — a thing this app must never do, and a
 * thing whose code has no business on the launch path.
 *
 * **Every editor in the window is one of these**, bound to the file it is given
 * rather than to "the active file": the tab strip renders one for whichever tab
 * is on top, and each `editors#<path>` pane renders one for its own file. The
 * options and the mount handler live here for that reason — two panes have to
 * render the *same* editor, and a component is a stronger way to say so than a
 * shared options object either caller could stop passing.
 *
 * **This component owns a view, not a buffer.** Two of these can be looking at
 * one file, so the two props are `pane` (which view am I) and `file` (what am I
 * looking at), and everything below follows from the split:
 *
 *  - the *model* — the buffer, its undo stack, its markers — belongs to the
 *    registry in `../monaco`, which is why `keepCurrentModel` is set: without
 *    it `@monaco-editor/react` disposes the model on unmount, and Monaco reacts
 *    to a disposed model by detaching it from every editor showing it and
 *    removing their DOM nodes. Closing one pane blanked the other
 *    (`e2e/editorPanes.spec.ts`);
 *  - the *view state* — scroll and cursor — belongs to this pane, so it is
 *    saved and restored under `pane`. `saveViewState` on `<Editor>` is turned
 *    off for exactly that reason: the library's own memory is keyed by path
 *    alone, which is the same "there is only one editor per file" assumption in
 *    miniature.
 */

import Editor, { type OnMount } from "@monaco-editor/react";

import {
  acquireModel,
  editorPathProp,
  languageForPath,
  MONO_FONT,
  monacoThemeName,
  recallViewState,
  releaseModel,
  rememberViewState,
} from "../monaco";
import { useStore, type OpenFile } from "../store";
import { useEffect, useRef } from "react";

/** The editor this instance created, so its own effects can reach it. */
type CodeEditorHandle = Parameters<OnMount>[0];

export default function CodeEditor({ pane, file }: { pane: string; file: OpenFile }) {
  const theme = useStore((s) => s.theme);

  const mounted = useRef<CodeEditorHandle | null>(null);
  /**
   * The path this editor is *currently displaying*, which is not always the
   * `file.path` of the render in flight.
   *
   * `<Editor>` swaps the model in its own effect, and a child's effects run
   * before its parent's — so during a tab switch there is a moment when the
   * editor already shows the new file while this ref still names the old one.
   * That is precisely what makes it the right thing to file the outgoing view
   * state under (see `onWillChangeModel` below), and it is why the ref is
   * advanced in an effect rather than during render.
   */
  const shown = useRef(file.path);
  /** Read inside Monaco listeners registered once, at mount — and
   * `@monaco-editor/react` keeps the `onMount` from the *first* render, so a
   * prop read through a closure there would be pinned to it. A pane's id is
   * fixed for its lifetime; this is what says so out loud. */
  const paneRef = useRef(pane);
  useEffect(() => {
    paneRef.current = pane;
  }, [pane]);

  /**
   * Remember where this pane was looking when it goes away, so reopening the
   * same pane on the same file lands where it left off.
   *
   * **Declared before the claim below on purpose.** React runs an unmount's
   * cleanups in declaration order, and releasing the last view of a file the
   * store has already closed disposes the model — after which there is no view
   * state left to read. Saving first makes this independent of that; the
   * `getModel()` guard is what catches it if the order ever changes.
   */
  useEffect(
    () => () => {
      const editor = mounted.current;
      if (editor !== null && editor.getModel() !== null) {
        rememberViewState(paneRef.current, shown.current, editor.saveViewState());
      }
    },
    [],
  );

  /**
   * One view's claim on the model, taken for as long as this editor displays
   * that path.
   *
   * Releasing does **not** dispose it: the file is still open in the tab strip,
   * with its undo history and its markers, and a pane closing is a view closing
   * (`../monaco`). Disposal happens when the store closes the *file*.
   */
  useEffect(() => {
    acquireModel(file.path);
    return () => releaseModel(file.path);
  }, [file.path]);

  /**
   * Put this pane back where it was looking, after `<Editor>` has swapped the
   * model in. A parent effect runs after the child's, which is the ordering
   * this needs — and the mount case is handled in `onMount` instead, because
   * the editor does not exist yet the first time this runs.
   */
  useEffect(() => {
    const editor = mounted.current;
    if (editor !== null && shown.current !== file.path) {
      editor.restoreViewState(recallViewState(paneRef.current, file.path));
    }
    shown.current = file.path;
  }, [file.path]);

  const onMount: OnMount = (editor, monaco) => {
    mounted.current = editor;
    editor.restoreViewState(recallViewState(paneRef.current, shown.current));
    // The outgoing half of the tab-strip switch: fired *before* the model is
    // swapped, while the live view state still describes the file named by
    // `shown`. The incoming half is the effect above.
    editor.onWillChangeModel(() => {
      rememberViewState(paneRef.current, shown.current, editor.saveViewState());
    });
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      const s = useStore.getState();
      if (s.activePath) void s.saveFile(s.activePath);
    });
  };

  return (
    <Editor
      path={editorPathProp(file.path)}
      defaultValue={file.buffer}
      defaultLanguage={languageForPath(file.path)}
      theme={monacoThemeName(theme)}
      onMount={onMount}
      // The model outlives this view — the registry decides when it does not.
      keepCurrentModel
      // …and so does the view state, per pane rather than per path (see above).
      saveViewState={false}
      onChange={(value) => {
        if (value !== undefined) useStore.getState().updateBuffer(file.path, value);
      }}
      loading={<div className="wb-editor-message">Loading editor…</div>}
      options={{
        fontSize: 13,
        lineHeight: 20,
        fontFamily: MONO_FONT,
        fontLigatures: false,
        minimap: { enabled: false },
        automaticLayout: true,
        scrollBeyondLastLine: false,
        fixedOverflowWidgets: true,
        padding: { top: 8, bottom: 8 },
        renderLineHighlight: "line",
      }}
    />
  );
}
