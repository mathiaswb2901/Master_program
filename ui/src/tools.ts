/**
 * The registered tools — the one shared file a new capability touches.
 *
 * Order is meaningful and is the *only* thing decided here: it is the panel
 * order in the default layout, the `Ctrl+1..N` focus order, the order of
 * command rows in the QuickBar, and the order of status-bar items inside a
 * region. Everything else about a tool lives in the tool's own module.
 *
 * Registration is static on purpose (see `registry.ts`): one array, resolved at
 * build time. The list is what a dynamic loader would later append to, not a
 * mechanism it would replace.
 */

import type { WorkbenchTool } from "./registry";

import { commandRelayTool } from "./commandRelay";
import { activityTool } from "./panels/ActivityPanel";
import { agentTool } from "./panels/AgentPanel";
import { conversationsTool } from "./panels/Conversations";
import { editorTool } from "./panels/EditorArea";
import { filesTool } from "./panels/FileTree";
import { keyboardTool } from "./panels/Keyboard";
import { layoutsTool } from "./panels/Layouts";
import { missionTool } from "./panels/MissionControl";
import { officeHostTool } from "./panels/OfficeHostPanel";
import { officeTool } from "./panels/OfficePanel";
import { panesTool } from "./panels/Panes";
import { scratchpadTool } from "./panels/Scratchpad";
import { terminalTool } from "./panels/Terminal";
import { usageTool } from "./panels/UsagePanel";
import { visualTool } from "./panels/VisualPanel";
import { workspacesTool } from "./panels/Workspaces";

export const TOOLS: readonly WorkbenchTool[] = [
  // First, and it contributes no panel: it decides which *project* every panel
  // below is looking at, so its status chip belongs at the left end of the bar
  // where the workspace name has always been.
  workspacesTool,
  filesTool,
  editorTool,
  agentTool,
  // After the Agent, because it is a way *into* one: a row here opens an agent
  // pane, and the two read as one capability seen from two distances.
  conversationsTool,
  terminalTool,
  // Before the OnlyOffice tool, and that is the registration: `documentViewFor`
  // takes the first enabled tool offering a view for a kind, so the native host
  // claims `office` and renders OnlyOffice itself wherever it cannot dock a
  // real window. Swapping these two lines puts the app back on OnlyOffice.
  officeHostTool,
  officeTool,
  scratchpadTool,
  usageTool,
  activityTool,
  // After Activity and Usage, because it is the board *over* them: it renders
  // their rows rather than deriving its own, so it reads as the wide view of
  // the two capabilities above it.
  missionTool,
  // Opened on demand from a plan card's Expand: one pane per drawn artifact,
  // rendering the same scene graph the card does (M5 item 3, PR 4).
  visualTool,
  // After every capability it describes, and before the two that arrange them:
  // its reference is a rendering of everything above, and its status chip sits
  // just inside the layout chip at the bar's outer edge.
  keyboardTool,
  // The last two contribute no panel — they arrange the ones above, so they
  // come after everything they can arrange. Panes splits the window and moves
  // between the pieces; Layouts remembers the result. Layouts is last so its
  // status chip sits at the outer edge of the bar, and both QuickBar sections
  // read as the window's own rather than as one capability's.
  panesTool,
  layoutsTool,
  // Contributes no panel, command or status item — only a lifecycle. It
  // publishes the invocable-command manifest on connect and runs a relayed
  // command on a `command_invoke` event (M5 item 14). Last because order here is
  // panel/focus/status order, and it is none of those; its position is inert.
  commandRelayTool,
];
