/**
 * Wire types. Mirrors server/src/workbench_server/models/{files,terminal,agents,plans}.py —
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

export interface CreateRequest {
  path: string;
  kind: "file" | "dir";
}

export interface RenameRequest {
  path: string;
  new_path: string;
}

export interface OkResponse {
  ok: true;
}

export interface FileChangedEvent {
  type: "file_changed";
  path: string;
  change: "added" | "modified" | "deleted";
  /** null for deletions and unreadable/binary-too-large files. */
  hash: string | null;
  origin: "watcher";
}

/** Everything that arrives on /ws/events (see also SessionStatusEvent below). */
export type WorkspaceEvent = FileChangedEvent | SessionStatusEvent | ShortcutsChangedEvent;

// ---- shortcuts.py -----------------------------------------------------------
// One markdown file per scope (workspace + user-global), merged. An entry is
// INSERTED into a surface, never executed — there is no "run" field by design.

export type ShortcutKind = "shell" | "prompt";
export type ShortcutSource = "workspace" | "global";

export interface ShortcutEntry {
  name: string;
  kind: ShortcutKind;
  body: string;
  /** Single chord ("Alt+G"); null = reachable from the QuickBar only. */
  keys: string | null;
  detail: string | null;
  source: ShortcutSource;
}

export interface ShortcutProblem {
  /** Display label of the file it came from. */
  file: string;
  message: string;
}

export interface ShortcutsState {
  entries: ShortcutEntry[];
  problems: ShortcutProblem[];
}

/** Broadcast on /ws/events when a shortcuts file loads differently than before. */
export interface ShortcutsChangedEvent {
  type: "shortcuts_changed";
  entry_count: number;
  problem_count: number;
}

// ---- office.py --------------------------------------------------------------
// Serialized with Pydantic aliases — camelCase on the wire where aliased.

export type OfficeDocumentType = "word" | "cell" | "slide";

export interface OfficeStatus {
  enabled: boolean;
  /** Base URL of the OnlyOffice Document Server (for loading api.js). */
  server_url: string | null;
}

export interface OfficeDocument {
  fileType: string;
  key: string;
  title: string;
  url: string;
}

export interface OfficeUser {
  id: string;
  name: string;
}

export interface OfficeEditorConfig {
  callbackUrl: string;
  mode: "edit" | "view";
  lang: string;
  user: OfficeUser;
  customization: Record<string, unknown>;
}

/** The object handed whole to `new DocsAPI.DocEditor(elementId, config)`. */
export interface DocEditorConfig {
  document: OfficeDocument;
  documentType: OfficeDocumentType;
  editorConfig: OfficeEditorConfig;
  /** JWT of this config (minus the token field itself). */
  token: string;
}

export interface ForcesaveRequest {
  path: string;
}

export interface CallbackResponse {
  error: number;
}

export interface OfficeLastSave {
  /** Hash of the editor's last saved bytes; null when it has never saved. */
  hash: string | null;
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

// ---- plans.py ---------------------------------------------------------------
// The present_plan primitive: a closed schema our own components render — never
// free-form markup from the model.

export interface FileRef {
  /** Workspace-relative, forward slashes. */
  path: string;
}

export interface PlanOption {
  option_id: string;
  label: string;
  pros: string[];
  cons: string[];
  /** At most one per group (validated server-side) — the only accented option. */
  recommended: boolean;
}

export interface OptionGroupNode {
  kind: "option_group";
  node_id: string;
  prompt: string;
  options: PlanOption[];
}

export interface PlanStep {
  text: string;
  file_refs: FileRef[];
}

export interface StepListNode {
  kind: "step_list";
  node_id: string;
  steps: PlanStep[];
}

export interface QuestionNode {
  kind: "question";
  node_id: string;
  text: string;
}

export interface MarkdownNode {
  kind: "markdown";
  node_id: string;
  text: string;
}

export type PlanNode = OptionGroupNode | StepListNode | QuestionNode | MarkdownNode;

export interface PlanArtifact {
  plan_id: string;
  title: string;
  summary: string;
  /** At most 15 (server-capped). */
  nodes: PlanNode[];
}

/** "no_decision" is timeout/interrupt — never an implied approval. */
export type PlanVerdict = "approve" | "revise" | "reject" | "no_decision";

export interface PlanAnnotation {
  node_id: string;
  text: string;
}

export interface PlanResponse {
  plan_id: string;
  verdict: PlanVerdict;
  /** node_id -> option_id for every option group the user resolved. */
  choices: Record<string, string>;
  annotations: PlanAnnotation[];
  comment: string;
}

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

/** Broadcast on /ws/events whenever a live session changes state — so sessions
 * without an open agent socket update too. */
export interface SessionStatusEvent {
  type: "session_status";
  session_id: string;
  folder: string;
  state: SessionState;
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

export interface PlanDecision {
  type: "plan_decision";
  response: PlanResponse;
}

export interface Interrupt {
  type: "interrupt";
}

export type AgentClientMessage = UserMessage | PermissionDecision | PlanDecision | Interrupt;

// server -> client over /ws/agent/{id}

export interface TextDelta {
  type: "text_delta";
  text: string;
}

export interface ToolUseNote {
  type: "tool_use";
  /** Stable call id; `tool_settled` refers back to it. */
  id: string;
  tool: string;
  summary: string;
}

/** The result for one tool call: settles exactly that row, not the whole turn. */
export interface ToolSettled {
  type: "tool_settled";
  id: string;
  ok: boolean;
  /** Truncated server-side (see TOOL_EXCERPT_LIMIT). */
  output_excerpt: string;
}

export interface PermissionRequest {
  type: "permission_request";
  request_id: string;
  tool: string;
  description: string;
}

export interface PlanPresented {
  type: "plan_presented";
  plan: PlanArtifact;
}

/** The pending plan settled server-side (decision, timeout, or interrupt). This
 * frame — not the local click — is what makes a card read-only, so a stale card
 * can never claim an approval the agent never received. */
export interface PlanResolved {
  type: "plan_resolved";
  plan_id: string;
  verdict: PlanVerdict;
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
  | ToolSettled
  | PermissionRequest
  | PlanPresented
  | PlanResolved
  | StatusChange
  | TurnDone
  | AgentError;
