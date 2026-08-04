/**
 * Wire types. Mirrors server/src/workbench_server/models/{files,terminal,agents}.py —
 * keep in lockstep with the Pydantic models.
 */

// ---- files.py ---------------------------------------------------------------

export interface TreeNode {
  name: string;
  /** Workspace-relative, forward slashes; "" = root. */
  path: string;
  kind: "file" | "dir";
  /** null for files, list (possibly empty) for dirs. */
  children: TreeNode[] | null;
}

export interface FileContent {
  path: string;
  content: string;
  /** sha256 of the bytes on disk. */
  hash: string;
}

export interface WriteRequest {
  path: string;
  content: string;
  /** If set, write is rejected with 409 when the on-disk hash no longer matches. */
  expected_hash?: string | null;
}

export interface WriteResponse {
  path: string;
  hash: string;
}

export interface FileChangedEvent {
  type: "file_changed";
  path: string;
  change: "added" | "modified" | "deleted";
  /** null for deletions and unreadable/binary-too-large files. */
  hash: string | null;
  origin: "watcher";
}

// ---- terminal.py ------------------------------------------------------------

export interface TerminalInput {
  type: "input";
  data: string;
}

export interface TerminalResize {
  type: "resize";
  cols: number;
  rows: number;
}

export type TerminalClientMessage = TerminalInput | TerminalResize;

export interface TerminalOutput {
  type: "output";
  data: string;
}

export interface TerminalExit {
  type: "exit";
}

export type TerminalServerMessage = TerminalOutput | TerminalExit;

// ---- agents.py --------------------------------------------------------------

export type SessionState = "idle" | "working" | "needs_attention";

export interface SessionInfo {
  session_id: string;
  /** Workspace-relative folder the session is bound to ("" = root). */
  folder: string;
  state: SessionState;
  /** true = running in the server process; false = resumable transcript on disk. */
  live: boolean;
  title: string;
  /** Unix mtime of the transcript (or creation time for live sessions). */
  updated_at: number;
}

export interface FolderSessions {
  folder: string;
  sessions: SessionInfo[];
}

export interface CreateSessionRequest {
  folder: string;
  resume_session_id?: string | null;
}

export interface TranscriptMessage {
  role: "user" | "assistant";
  text: string;
}

export interface TranscriptResponse {
  session_id: string;
  messages: TranscriptMessage[];
}

export interface UiState {
  active_file: string | null;
  open_files: string[];
  dirty_files: string[];
}

// client -> server over /ws/agent/{id}

export interface UserMessage {
  type: "user_message";
  text: string;
}

export interface PermissionDecision {
  type: "permission_decision";
  request_id: string;
  allow: boolean;
}

export interface Interrupt {
  type: "interrupt";
}

export type AgentClientMessage = UserMessage | PermissionDecision | Interrupt;

// server -> client over /ws/agent/{id}

export interface TextDelta {
  type: "text_delta";
  text: string;
}

export interface ToolUseNote {
  type: "tool_use";
  tool: string;
  summary: string;
}

export interface PermissionRequest {
  type: "permission_request";
  request_id: string;
  tool: string;
  description: string;
}

export interface StatusChange {
  type: "status";
  session_id: string;
  state: SessionState;
}

export interface TurnDone {
  type: "turn_done";
  session_id: string;
  cost_usd: number | null;
  is_error: boolean;
}

export interface AgentError {
  type: "agent_error";
  message: string;
}

export type AgentServerMessage =
  | TextDelta
  | ToolUseNote
  | PermissionRequest
  | StatusChange
  | TurnDone
  | AgentError;
