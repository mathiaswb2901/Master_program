import type { DockviewPanelApi, IDockviewPanelProps } from "dockview";
import { Fragment, lazy, Suspense, useEffect, useRef } from "react";

import { focusPanel } from "../dock";
import { loadMonaco, prefetchMonaco } from "../monaco";
import { paneInstance } from "../panes";
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
 * theme are all configured before the first `<Editor>` mounts. It passes no
 * theme: which theme is current is a question only the moment the bundle lands
 * can answer, and `loadMonaco` asks it there (see `monaco.ts`).
 *
 * **Every Monaco instance in the app is mounted through this** — the tab
 * strip's and every file pane's. One component rather than one shared options
 * object is what keeps two editors the *same* editor rather than two that
 * merely look alike, and it is what keeps `@monaco-editor/react` (and the CDN
 * loader it drags with it) out of this module's imports, which is the whole
 * point: a static import anywhere in this graph puts 3.3 MB back on the launch
 * path (`ui/e2e/perf/bundle.spec.ts`).
 */
const CodeEditor = lazy(async () => {
  await loadMonaco();
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

/**
 * The Editor tool renders two ways, decided by the pane's id (`../panes.ts`).
 *
 *  - `editors` — the default pane: the tab strip over every open file. The
 *    panel Workbench has always had, and still the home of the `keepMounted`
 *    document views (an OnlyOffice instance is too expensive to tear down on a
 *    tab switch);
 *  - `editors#<path>` — a pane showing one file, side by side with another.
 *    The path is workspace-relative, which is what makes a saved layout come
 *    back on the same two files rather than on two empty editors.
 */
export function EditorAreaPanel(props: IDockviewPanelProps) {
  const path = paneInstance(props.api.id);
  return path === null ? <EditorTabs /> : <FilePane path={path} api={props.api} />;
}

/** One file, filling its pane. */
function FilePane({ path, api }: { path: string; api: DockviewPanelApi }) {
  const file = useStore((s) => s.openFiles.find((f) => f.path === path));

  /**
   * Open it **once**, when the pane appears — a pane restored from a layout
   * names a file nothing has read yet.
   *
   * Once, and not "whenever it is missing": the tab strip can close this file
   * too, and a pane that reopened whatever it lost would make that close button
   * do nothing. Closed from elsewhere, the pane says so and offers it back.
   */
  const openedFor = useRef<string | null>(null);
  useEffect(() => {
    if (openedFor.current === path) return;
    openedFor.current = path;
    if (!useStore.getState().openFiles.some((f) => f.path === path)) {
      void useStore.getState().openFile(path);
    }
  }, [path]);

  // Ctrl+S saves the file you are looking at, so the focused pane decides which
  // one that is — the same rule the session panes follow.
  useEffect(() => {
    if (api.isActive) useStore.getState().setActiveFile(path);
    const subscription = api.onDidActiveChange((event) => {
      if (event.isActive) useStore.getState().setActiveFile(path);
    });
    return () => subscription.dispose();
  }, [api, path]);

  if (file === undefined) {
    return (
      <div className="wb-pane-single">
        <div className="wb-pane-note">
          <span className="wb-pane-note-msg u-truncate">{path} is closed.</span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => void useStore.getState().openFile(path)}
          >
            Reopen
          </button>
        </div>
      </div>
    );
  }
  const view = documentViewFor(TOOLS, file.kind);
  return (
    <div className="wb-pane-single">
      {file.loadError !== null ? (
        <div className="wb-editor-message">
          Cannot open {file.name}: {file.loadError}
        </div>
      ) : view !== null ? (
        // A view that must stay mounted is one instance per open file, owned by
        // the tab strip (a second OnlyOffice editor on the same document is a
        // co-editing session with yourself). Say so rather than opening one.
        view.keepMounted === true ? (
          <div className="wb-pane-note">
            <span className="wb-pane-note-msg u-truncate">
              {file.name} opens in the Editor pane.
            </span>
            <button
              type="button"
              className="wb-btn wb-btn-sm wb-btn-outline"
              onClick={() => {
                useStore.getState().setActiveFile(path);
                focusPanel("editors");
              }}
            >
              Show it
            </button>
          </div>
        ) : (
          <div className={view.hostClassName}>
            <view.component file={file} />
          </div>
        )
      ) : (
        <div className="wb-editor-body">
          {/* Same lazy surface the tab strip mounts, and the same fallback: a
              pane split onto a file is often the *first* editor in the window,
              so it has to be able to wait for the chunk too. */}
          <Suspense fallback={<div className="wb-editor-message">Loading editor…</div>}>
            <CodeEditor file={file} />
          </Suspense>
        </div>
      )}
    </div>
  );
}

function EditorTabs() {
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
  // 1,052 ms. The root listing is the last thing the launch path waits for, so
  // its arrival is the signal that the editor's bytes are now free to fetch.
  // Two conditions, both necessary: this panel exists (a saved layout without
  // an editor warms nothing) and the launch is over.
  //
  // **The signal is `dirs[""]`, the same one the tree renders from**, and it
  // has to be: `store.tree` is no longer the launch listing but the *search
  // index*, which nothing fetches until the QuickBar asks for it. Warming on
  // that is warming never — the chunk is not requested at all, and the whole
  // cost lands back on the user's first click (the failure `ui/e2e/theme.spec.ts`
  // and `ui/e2e/perf/open-file.spec.ts` both catch).
  //
  // The strip is also where the *only* prefetch lives: a file pane restored
  // from a layout mounts an editor immediately, so it needs no warming, and
  // `prefetchMonaco` is idempotent for the window where both happen.
  const workspaceLoaded = useStore((s) => s.dirs[""] !== undefined);
  useEffect(() => {
    if (workspaceLoaded) prefetchMonaco();
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
    // Plural: two files side by side is the oldest reason anyone splits a
    // window. The instance key is the workspace-relative path.
    singleton: false,
    instances: {
      // Open files only. "Every file in the workspace" is what the QuickBar's
      // own file search is for, and offering 5,000 rows here would bury the
      // sessions and the terminals under them.
      options: () =>
        useStore
          .getState()
          .openFiles.map((file) => ({
            id: `editors.${file.path}`,
            title: file.name,
            detail: file.path.includes("/") ? file.path.slice(0, file.path.lastIndexOf("/")) : "",
            category: "Open files",
            key: () => file.path,
          })),
      titleFor: (key) => key.split("/").pop() ?? key,
    },
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
