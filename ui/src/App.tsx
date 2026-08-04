import {
  DockviewReact,
  type DockviewReadyEvent,
  type IDockviewPanelHeaderProps,
  type IDockviewPanelProps,
} from "dockview";
import { useEffect, type FunctionComponent } from "react";

import { AgentPanel } from "./panels/AgentPanel";
import { EditorAreaPanel } from "./panels/EditorArea";
import { FileTreePanel } from "./panels/FileTree";
import { QuickBar } from "./panels/QuickBar";
import { TerminalPanel } from "./panels/Terminal";
import { useStore } from "./store";

const components: Record<string, FunctionComponent<IDockviewPanelProps>> = {
  files: FileTreePanel,
  editors: EditorAreaPanel,
  agent: AgentPanel,
  terminal: TerminalPanel,
};

/** Fixed panels: title only, no close button — closing them would strand the app. */
function PanelTab(props: IDockviewPanelHeaderProps) {
  return <div className="wb-panel-tab u-truncate">{props.api.title ?? props.api.id}</div>;
}

const WORKBENCH_THEME = { name: "workbench", className: "dockview-theme-workbench" };

function onReady(event: DockviewReadyEvent): void {
  const { api } = event;
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
  useEffect(() => {
    useStore.getState().init();
  }, []);

  useEffect(() => {
    // Capture phase so Ctrl+K / Ctrl+P / Ctrl+S win over Monaco and xterm.
    const onKey = (e: KeyboardEvent): void => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const key = e.key.toLowerCase();
      const s = useStore.getState();
      if (key === "k" || key === "p") {
        e.preventDefault();
        e.stopPropagation();
        s.setQuickBarOpen(!s.quickBarOpen);
      } else if (key === "s") {
        e.preventDefault();
        e.stopPropagation();
        if (s.activePath !== null) void s.saveFile(s.activePath);
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

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
      <QuickBar />
    </div>
  );
}
