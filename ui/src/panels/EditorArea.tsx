import Editor, { type OnMount } from "@monaco-editor/react";
import type { IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import {
  editorPathProp,
  languageForPath,
  MONO_FONT,
  monacoThemeName,
  setActiveEditor,
} from "../monaco";
import { useStore, type OpenFile } from "../store";

import { OfficePanel } from "./OfficePanel";

function EditorTab({ file, active }: { file: OpenFile; active: boolean }) {
  return (
    <div
      className={
        "wb-editor-tab" + (active ? " is-active" : "") + (file.dirty ? " is-dirty" : "")
      }
      role="tab"
      aria-selected={active}
    >
      <button
        type="button"
        className="wb-editor-tab-label u-truncate"
        title={file.path}
        onClick={() => useStore.getState().setActiveFile(file.path)}
      >
        {file.name}
      </button>
      <span className="wb-editor-tab-trailing">
        {file.dirty && <span className="wb-dirty-dot" title="Unsaved changes" />}
        <button
          type="button"
          className="wb-tab-close"
          aria-label={`Close ${file.name}`}
          onClick={() => useStore.getState().closeFile(file.path)}
        >
          ×
        </button>
      </span>
    </div>
  );
}

function ConflictBar({ file }: { file: OpenFile }) {
  return (
    <div className="wb-conflict-bar" role="alert">
      <span className="wb-conflict-msg u-truncate">{file.conflict}</span>
      <button
        type="button"
        className="wb-btn wb-btn-sm wb-btn-outline"
        onClick={() => void useStore.getState().reloadFromDisk(file.path)}
      >
        Reload
      </button>
      <button
        type="button"
        className="wb-btn wb-btn-sm wb-btn-ghost"
        onClick={() => void useStore.getState().keepMine(file.path)}
      >
        Keep mine
      </button>
    </div>
  );
}

export function EditorAreaPanel(_props: IDockviewPanelProps) {
  const openFiles = useStore((s) => s.openFiles);
  const activePath = useStore((s) => s.activePath);
  const theme = useStore((s) => s.theme);
  const active = openFiles.find((f) => f.path === activePath) ?? null;

  useEffect(() => () => setActiveEditor(null), []);

  const onMount: OnMount = (editor, monaco) => {
    setActiveEditor(editor);
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      const s = useStore.getState();
      if (s.activePath) void s.saveFile(s.activePath);
    });
  };

  if (openFiles.length === 0) {
    return (
      <div className="wb-editor">
        <div className="wb-empty">
          <div className="wb-empty-title">No file open</div>
          <div className="wb-empty-hint">
            Open a file — <span className="wb-keycap">Ctrl</span>{" "}
            <span className="wb-keycap">P</span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="wb-editor">
      <div className="wb-editor-tabs" role="tablist">
        {openFiles.map((f) => (
          <EditorTab key={f.path} file={f} active={f.path === activePath} />
        ))}
      </div>
      {active?.kind === "text" && active.conflict !== null && <ConflictBar file={active} />}
      <div className="wb-editor-body">
        {active === null ? null : active.kind === "office" ? (
          // Keyed by path so switching office tabs tears down and recreates
          // the editor instance instead of reusing another document's iframe.
          <OfficePanel key={active.path} file={active} />
        ) : active.loadError !== null ? (
          <div className="wb-editor-message">Cannot open {active.name}: {active.loadError}</div>
        ) : (
          <Editor
            path={editorPathProp(active.path)}
            defaultValue={active.buffer}
            defaultLanguage={languageForPath(active.path)}
            theme={monacoThemeName(theme)}
            onMount={onMount}
            onChange={(value) => {
              if (value !== undefined) useStore.getState().updateBuffer(active.path, value);
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
        )}
      </div>
    </div>
  );
}
