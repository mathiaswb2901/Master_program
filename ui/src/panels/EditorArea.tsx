import type { IDockviewPanelProps } from "dockview";
import { Fragment, lazy, Suspense, useEffect } from "react";

import { focusPanel } from "../dock";
import { loadMonaco, prefetchMonaco } from "../monaco";
import { documentViewFor, documentViews, type WorkbenchTool } from "../registry";
import { relativeTimePhrase } from "../relativeTime";
import { useStore, type OpenFile } from "../store";
import { TOOLS } from "../tools";

/**
 * Monaco arrives on the first text file, not at startup.
 *
 * `React.lazy` rather than a mechanism of our own — and at *this* level rather
 * than on the tool's registered `panel.component`, deliberately. The editor
 * panel is in the startup layout, so dockview asks for its component during the
 * first render either way; lazy-loading the panel would defer nothing and would
 * hand dockview a component that suspends with no boundary around it. The split
 * that pays is inside the panel, between the chrome (instant) and the editor
 * (expensive), which is why the registry needed no change to allow this.
 *
 * The factory awaits `loadMonaco` before importing the surface so that
 * `MonacoEnvironment`, the `@monaco-editor/react` loader and the workbench
 * theme are all configured before the first `<Editor>` mounts.
 */
const CodeEditor = lazy(async () => {
  await loadMonaco(useStore.getState().theme);
  return import("./CodeEditor");
});

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
 * opens the session).
 *
 * Deliberately *not* gated on `acknowledged`, unlike the two dots. The dots
 * mean "you have not looked at this yet" and opening the file answers them; the
 * bar answers a different question — "who wrote what I am reading?" — which
 * stays worth answering every time the file is opened, and is the only place
 * the link back to that conversation lives. It ends by itself when the claim
 * does: any later change from anywhere else clears the entry and the bar with
 * it. Dismiss closes it for good (persisted, see the store), for a file the
 * user has decided about. Documented in DESIGN.md §6.1.
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
  const active = openFiles.find((f) => f.path === activePath) ?? null;
  // What renders a non-text buffer is a registry question, not a list of file
  // kinds here: the Office tool claims `office`, and the native Office host
  // will claim it back the same way.
  const views = documentViews(TOOLS);
  const activeView = active === null ? null : documentViewFor(TOOLS, active.kind);

  // Warm the editor chunk, but not until the window has finished starting.
  //
  // Mounting is the wrong moment, and it was measured to be: between first
  // paint and the workspace listing arriving the main thread is *idle*, so
  // `requestIdleCallback` fires immediately and a 3.3 MB parse lands on top of
  // the tree render — a warm launch went from 662 ms to a clickable tree to
  // 1,052 ms. The tree is the last thing the launch path waits for, so its
  // arrival is the signal that the editor's bytes are now free to fetch. Two
  // conditions, both necessary: this panel exists (a saved layout without an
  // editor warms nothing) and the launch is over.
  const workspaceLoaded = useStore((s) => s.tree !== null);
  useEffect(() => {
    if (workspaceLoaded) prefetchMonaco(useStore.getState().theme);
  }, [workspaceLoaded]);

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
      {active !== null && activeView === null && active.conflict !== null && (
        <ConflictBar file={active} />
      )}
      <div className="wb-editor-body">
        {/* A `keepMounted` view keeps every open file of its kind mounted for
            the tab's whole lifetime — creating an OnlyOffice instance is
            expensive, so tab switches only toggle CSS visibility. display:none
            keeps hidden iframes out of focus/tab order and unable to steal
            keystrokes. Unmount happens only on close or generation bump. */}
        {views
          .filter((view) => view.keepMounted === true)
          .map((view) => (
            <Fragment key={view.kind}>
              {openFiles
                .filter((f) => f.kind === view.kind)
                .map((f) => (
                  <div
                    key={f.path}
                    className={view.hostClassName + (f.path === activePath ? "" : " is-hidden")}
                  >
                    <view.component file={f} />
                  </div>
                ))}
            </Fragment>
          ))}
        {active !== null && activeView !== null && activeView.keepMounted !== true && (
          <div className={activeView.hostClassName}>
            <activeView.component file={active} />
          </div>
        )}
        {active === null || activeView !== null ? null : active.loadError !== null ? (
          <div className="wb-editor-message">Cannot open {active.name}: {active.loadError}</div>
        ) : (
          // Same message the `<Editor>`'s own `loading` prop shows, so the two
          // waits a first open can involve — fetching the chunk, then creating
          // the editor — read as one.
          <Suspense fallback={<div className="wb-editor-message">Loading editor…</div>}>
            <CodeEditor file={active} />
          </Suspense>
        )}
      </div>
    </div>
  );
}

// ---- registration -----------------------------------------------------------

/** Middle of the status bar's left group: the file you are looking at. */
function ActiveFileStatus() {
  const activePath = useStore((s) => s.activePath);
  const dirty = useStore((s) => s.openFiles.find((f) => f.path === s.activePath)?.dirty ?? false);
  if (activePath === null) return null;
  return (
    <>
      <span className="wb-status-sep" aria-hidden="true">
        /
      </span>
      <span className="wb-status-file u-truncate" title={activePath}>
        {activePath}
      </span>
      {dirty && (
        <span
          className="wb-status-dirty"
          role="img"
          aria-label="Unsaved changes"
          title="Unsaved changes"
        />
      )}
    </>
  );
}

function cycleEditorTab(step: number): void {
  const s = useStore.getState();
  if (s.openFiles.length === 0) return;
  const current = s.openFiles.findIndex((f) => f.path === s.activePath);
  const index = (Math.max(current, 0) + step + s.openFiles.length) % s.openFiles.length;
  const next = s.openFiles[index];
  if (next !== undefined) s.setActiveFile(next.path);
}

const hasOpenFile = (): boolean => useStore.getState().openFiles.length > 0;
const hasActiveFile = (): boolean => useStore.getState().activePath !== null;

export const editorTool: WorkbenchTool = {
  id: "editors",
  title: "Editor",
  panel: {
    component: EditorAreaPanel,
    defaultLocation: { area: "center" },
  },
  commands: [
    {
      id: "file.save",
      title: "Save file",
      when: hasActiveFile,
      run: () => {
        const s = useStore.getState();
        if (s.activePath !== null) void s.saveFile(s.activePath);
      },
    },
    {
      id: "editor.nextTab",
      title: "Next editor tab",
      when: hasOpenFile,
      run: () => cycleEditorTab(1),
    },
    {
      id: "editor.prevTab",
      title: "Previous editor tab",
      when: hasOpenFile,
      run: () => cycleEditorTab(-1),
    },
    {
      id: "editor.close",
      title: "Close editor tab",
      when: hasActiveFile,
      run: () => {
        const path = useStore.getState().activePath;
        if (path !== null) useStore.getState().requestCloseFile(path);
      },
    },
  ],
  shortcuts: {
    "file.save": ["Ctrl+S"],
    "editor.nextTab": ["Ctrl+PageDown", "Alt+PageDown"],
    "editor.prevTab": ["Ctrl+PageUp", "Alt+PageUp"],
    "editor.close": ["Alt+W", "Ctrl+F4"],
  },
  statusContributions: [{ region: "left", component: ActiveFileStatus }],
};
