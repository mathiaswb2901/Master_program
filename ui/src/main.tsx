import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource-variable/jetbrains-mono";
import "dockview-react/dist/styles/dockview.css";
import "@xterm/xterm/css/xterm.css";
import "./design/tokens.css";
import "./styles/app.css";
import "./styles/dockview.css";
import "./styles/filetree.css";
import "./styles/editor.css";
import "./styles/office.css";
import "./styles/terminal.css";
import "./styles/agent.css";
import "./styles/voice.css";
import "./styles/plan.css";
import "./styles/visual.css";
import "./styles/quickbar.css";
import "./styles/statusbar.css";
import "./styles/overlays.css";

import { createRoot } from "react-dom/client";

import App from "./App";

/**
 * Render as soon as this module is ready.
 *
 * This used to `await` `window.load` first, so that surfaces which compute
 * their colors from the CSS tokens (Monaco, xterm) could not read them before
 * the stylesheet applied. `load` waits for the *last* subresource, which in
 * practice meant first paint was gated on four webfont files — 200 ms of doing
 * nothing, on every start, to protect a read that was never at risk: a
 * classic-or-module script's execution already waits for the stylesheets
 * declared above it, and both surfaces are built later still (Monaco inside its
 * own dynamic import, xterm when its panel mounts).
 *
 * What the fonts can now do is arrive after the first paint and swap. That is a
 * layout-shift risk rather than a theoretical one, so it is measured instead of
 * argued about: `ui/e2e/perf/launch.spec.ts` asserts a cumulative layout shift
 * ceiling, and the fix if it is ever breached is `font-display`/`size-adjust`,
 * not this gate coming back.
 */
const root = document.getElementById("root");
if (!root) throw new Error("missing #root element");
createRoot(root).render(<App />);
