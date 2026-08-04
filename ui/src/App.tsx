import {
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelHeaderProps,
  type IDockviewPanelProps,
} from "dockview";
import { useEffect, type FunctionComponent } from "react";

import { installCommandKeys, setDockApi } from "./commands";
import { AgentPanel } from "./panels/AgentPanel";
import { EditorAreaPanel } from "./panels/EditorArea";
import { FileTreePanel } from "./panels/FileTree";
import { DirtyCloseModal } from "./panels/Modal";
import { QuickBar } from "./panels/QuickBar";
import { StatusBar } from "./panels/StatusBar";
import { TerminalPanel } from "./panels/Terminal";
import { Toasts } from "./panels/Toasts";
import { useStore } from "./store";

const components: Record<string, FunctionComponent<IDockviewPanelProps>> = {
  files: FileTreePanel,
  editors: EditorAreaPanel,
  agent: AgentPanel,
  terminal: TerminalPanel,
};

const BASE_TITLE = "Workbench";

const anyNeedsAttention = (states: Record<string, string>): boolean =>
  Object.values(states).some((state) => state === "needs_attention");

/** Fixed panels: title only, no close button — closing them would strand the app.
 * The Agent tab carries an aggregate attention dot (DESIGN.md §6.4 dot-only). */
function PanelTab(props: IDockviewPanelHeaderProps) {
  const attention = useStore(
    (s) => props.api.id === "agent" && anyNeedsAttention(s.sessionStates),
  );
  return (
    <div className="wb-panel-tab u-truncate">
      {props.api.title ?? props.api.id}
      {attention && (
        <span
          className="wb-tab-attention-dot"
          role="img"
          aria-label="A session needs attention"
          title="A session needs attention"
        />
      )}
    </div>
  );
}

const WORKBENCH_THEME = { name: "workbench", className: "dockview-theme-workbench" };

function onReady(event: DockviewReadyEvent): void {
  const { api } = event;
  // The registry needs the dock handle for the panel-focus commands (Ctrl+1..4).
  setDockApi(api);
  api.addPanel({ id: "editors", component: "editors", title: "Editor" });
  api.addPanel({
    id: "files",
    component: "files",
    title: "Files",
    position: { referencePanel: "editors", direction: "left" },
    initialWidth: 240,
  });
  api.addPanel({
    id: "agent",
    component: "agent",
    title: "Agent",
    position: { referencePanel: "editors", direction: "right" },
    initialWidth: 380,
  });
  api.addPanel({
    id: "terminal",
    component: "terminal",
    title: "Terminal",
    position: { referencePanel: "editors", direction: "below" },
    initialHeight: 260,
  });
  api.getPanel("editors")?.api.setActive();
}

export default function App() {
  const attention = useStore((s) => anyNeedsAttention(s.sessionStates));

  useEffect(() => {
    useStore.getState().init();
  }, []);

  // Attention badge in the window/taskbar title; cleared once attended.
  useEffect(() => {
    document.title = attention ? `● ${BASE_TITLE}` : BASE_TITLE;
  }, [attention]);

  // Never lose unsaved buffers to a silent window close/refresh.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent): void => {
      if (useStore.getState().openFiles.some((f) => f.dirty)) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  // Every keybinding in the app comes from the command registry (commands.ts).
  useEffect(() => installCommandKeys(), []);

  useEffect(() => () => setDockApi(null), []);

  return (
    <div className="wb-root">
      <div className="wb-dock">
        <DockviewReact
          components={components}
          defaultTabComponent={PanelTab}
          theme={WORKBENCH_THEME}
          onReady={onReady}
        />
      </div>
      <StatusBar />
      <QuickBar />
      <DirtyCloseModal />
      <Toasts />
    </div>
  );
}
