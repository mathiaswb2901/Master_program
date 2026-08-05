import Editor, { type OnMount } from "@monaco-editor/react";
import type { IDockviewPanelProps } from "dockview";
import { useEffect } from "react";

import { focusPanel } from "../commands";
import {
  editorPathProp,
  languageForPath,
  MONO_FONT,
  monacoThemeName,
  setActiveEditor,
} from "../monaco";
import { relativeTimePhrase } from "../relativeTime";
import { useStore, type OpenFile } from "../store";

import { OfficePanel } from "./OfficePanel";

/** Same marker as the tree row, for a tab the user has not looked at yet: an
 * agent can change a file that is open behind the active one. */
function TabAgentMark({ path }: { path: string }) {
  const entry = useStore((s) => s.provenance[path]);
  if (entry === undefined || entry.agent === null || entry.acknowledged) return null;
  const label = `Changed by ${entry.agent.session_title}`;
  return <span className="wb-tab-agent-dot" role="img" aria-label={label} title={label} />;
}

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
      <TabAgentMark path={file.path} />
      <span className="wb-editor-tab-trailing">
        {file.dirty && <span className="wb-dirty-dot" title="Unsaved changes" />}
        <button
          type="button"
          className="wb-tab-close"
          aria-label={`Close ${file.name}`}
          onClick={() => useStore.getState().requestCloseFile(file.path)}
        >
          ×
        </button>
      </span>
    </div>
  );
}

/**
 * One line above the buffer for a file an agent changed: who, with what tool,
 * how long ago — and a link back to that exact conversation, which is the other
 * half of the loop the chat's file links start (a tool row opens the file; this
 * opens the session). Shown while the attribution stands and the user has not
 * closed it; Dismiss also acknowledges, which is what clears the tree marker.
 */
function ProvenanceBar({ path }: { path: string }) {
  const entry = useStore((s) => s.provenance[path]);
  const dismissed = useStore((s) => s.provenanceDismissed[path] === true);
  const agent = entry?.agent ?? null;
  if (entry === undefined || agent === null || dismissed) return null;
  return (
    <div className="wb-provenance-bar">
      <span className="wb-provenance-dot" aria-hidden="true" />
      <span className="wb-provenance-msg u-truncate">
        Changed by{" "}
        <button
          type="button"
          className="wb-provenance-link"
          aria-label={`Open session ${agent.session_title}`}
          title={`Open session ${agent.session_title}`}
          onClick={() => {
            useStore.getState().openSessionById(agent.session_id);
            focusPanel("agent");
          }}
        >
          {agent.session_title}
        </button>{" "}
        · {agent.tool} ·{" "}
        <span className="u-tabular">{relativeTimePhrase(entry.changed_at)}</span>
      </span>
      <button
        type="button"
        className="wb-btn wb-btn-sm wb-btn-ghost"
        onClick={() => useStore.getState().dismissProvenance(path)}
      >
        Dismiss
      </button>
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
      {active !== null && <ProvenanceBar path={active.path} />}
      {active?.kind === "text" && active.conflict !== null && <ConflictBar file={active} />}
      <div className="wb-editor-body">
        {/* Every open office file keeps its editor mounted for the tab's whole
            lifetime — creating an OnlyOffice instance is expensive, so tab
            switches only toggle CSS visibility. display:none keeps hidden
            iframes out of focus/tab order and unable to steal keystrokes.
            Unmount (destroyEditor) happens only on close or generation bump. */}
        {openFiles
          .filter((f) => f.kind === "office")
          .map((f) => (
            <div
              key={f.path}
              className={"wb-office-host" + (f.path === activePath ? "" : " is-hidden")}
            >
              <OfficePanel file={f} />
            </div>
          ))}
        {active === null || active.kind === "office" ? null : active.loadError !== null ? (
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
