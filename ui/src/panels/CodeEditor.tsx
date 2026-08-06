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
 */

import Editor, { type OnMount } from "@monaco-editor/react";

import {
  clearActiveEditor,
  editorPathProp,
  languageForPath,
  MONO_FONT,
  monacoThemeName,
  setActiveEditor,
} from "../monaco";
import { useStore, type OpenFile } from "../store";
import { useEffect, useRef } from "react";

/** The editor this instance created, so its unmount can withdraw it. */
type CodeEditorHandle = Parameters<OnMount>[0];

export default function CodeEditor({ file }: { file: OpenFile }) {
  const theme = useStore((s) => s.theme);

  /**
   * The active editor is what `setModelContent` restores cursor and scroll
   * through, and *which* editor that is follows the caret rather than the last
   * mount: with a file pane beside the tab strip there can be several, and an
   * on-disk change must not restore some other pane's cursor (`monaco.ts`).
   *
   * The registration is withdrawn here rather than in the panel because the
   * panel outlives every editor in it — closing the last tab unmounts only
   * this — and it is withdrawn *by identity*: an editor going away says nothing
   * about the one in the pane next to it, which is still on screen and may well
   * be the one holding the caret.
   */
  const mounted = useRef<CodeEditorHandle | null>(null);
  useEffect(
    () => () => {
      if (mounted.current !== null) clearActiveEditor(mounted.current);
    },
    [],
  );

  const onMount: OnMount = (editor, monaco) => {
    mounted.current = editor;
    setActiveEditor(editor);
    editor.onDidFocusEditorText(() => setActiveEditor(editor));
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
