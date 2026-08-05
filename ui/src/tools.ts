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

import { agentTool } from "./panels/AgentPanel";
import { editorTool } from "./panels/EditorArea";
import { filesTool } from "./panels/FileTree";
import { layoutsTool } from "./panels/Layouts";
import { officeHostTool } from "./panels/OfficeHostPanel";
import { officeTool } from "./panels/OfficePanel";
import { panesTool } from "./panels/Panes";
import { scratchpadTool } from "./panels/Scratchpad";
import { terminalTool } from "./panels/Terminal";
import { usageTool } from "./panels/UsagePanel";

export const TOOLS: readonly WorkbenchTool[] = [
  filesTool,
  editorTool,
  agentTool,
  terminalTool,
  // Before the OnlyOffice tool, and that is the registration: `documentViewFor`
  // takes the first enabled tool offering a view for a kind, so the native host
  // claims `office` and renders OnlyOffice itself wherever it cannot dock a
  // real window. Swapping these two lines puts the app back on OnlyOffice.
  officeHostTool,
  officeTool,
  scratchpadTool,
  usageTool,
  // The last two contribute no panel — they arrange the ones above, so they
  // come after everything they can arrange. Panes splits the window and moves
  // between the pieces; Layouts remembers the result. Layouts is last so its
  // status chip sits at the outer edge of the bar, and both QuickBar sections
  // read as the window's own rather than as one capability's.
  panesTool,
  layoutsTool,
];
