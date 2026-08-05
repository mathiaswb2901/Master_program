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
import { officeTool } from "./panels/OfficePanel";
import { scratchpadTool } from "./panels/Scratchpad";
import { terminalTool } from "./panels/Terminal";

export const TOOLS: readonly WorkbenchTool[] = [
  filesTool,
  editorTool,
  agentTool,
  terminalTool,
  officeTool,
  scratchpadTool,
];
