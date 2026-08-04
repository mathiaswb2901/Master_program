import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource-variable/jetbrains-mono";
import "dockview/dist/styles/dockview.css";
import "@xterm/xterm/css/xterm.css";
import "./design/tokens.css";
import "./styles/app.css";
import "./styles/dockview.css";
import "./styles/filetree.css";
import "./styles/editor.css";
import "./styles/office.css";
import "./styles/terminal.css";
import "./styles/agent.css";
import "./styles/plan.css";
import "./styles/quickbar.css";
import "./styles/overlays.css";

import { createRoot } from "react-dom/client";

import App from "./App";
import { initMonaco } from "./monaco";
import { useStore } from "./store";

async function start(): Promise<void> {
  // Monaco/xterm themes are computed from the CSS tokens — make sure the
  // stylesheet is applied before anything reads computed values.
  if (document.readyState !== "complete") {
    await new Promise<void>((resolve) => {
      window.addEventListener("load", () => resolve(), { once: true });
    });
  }
  initMonaco(useStore.getState().theme);
  const root = document.getElementById("root");
  if (!root) throw new Error("missing #root element");
  createRoot(root).render(<App />);
}

void start();
