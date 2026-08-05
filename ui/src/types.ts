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
export type WorkspaceEvent =
  | FileChangedEvent
  | SessionStatusEvent
  | ShortcutsChangedEvent
  | FileProvenanceEvent
  | OfficeHostEvent;

// ---- provenance.py ----------------------------------------------------------
// Who last changed a file. `agent === null` is the honest "we do not know" —
// never a guess at the most recent session.

export interface AgentAttribution {
  session_id: string;
  session_title: string;
  /** The file-writing tool that named the path (Write/Edit/…). */
  tool: string;
}

export interface ProvenanceEntry {
  /** Workspace-relative, forward slashes — exactly as the watcher reports it. */
  path: string;
  /** Unix seconds. */
  changed_at: number;
  /** null = unattributed: the user, another editor, a git checkout. */
  agent: AgentAttribution | null;
  /** The user has opened or dismissed this change; the tree marker clears. */
  acknowledged: boolean;
}

/** Broadcast on /ws/events when a path's provenance changes. An entry whose
 * `agent` is null means "no agent claim on this path any more" — drop it from
 * the map rather than keep showing a claim that is no longer true. */
export interface FileProvenanceEvent {
  type: "file_provenance";
  entry: ProvenanceEntry;
}

/** GET /api/provenance. In-memory server-side: a restart returns it empty. */
export interface ProvenanceMap {
  entries: ProvenanceEntry[];
}

export interface AcknowledgeRequest {
  path: string;
}

// ---- shortcuts.py -----------------------------------------------------------
// One markdown file per scope (workspace + user-global), merged. Nothing an
// entry can do executes: `shell` and `prompt` are INSERTED into a surface, and
// `layout` names one of the user's own saved arrangements and moves panels.
// There is no "run" field by design.

export type ShortcutKind = "shell" | "prompt" | "layout";
export type ShortcutSource = "workspace" | "global";

export interface ShortcutEntry {
  name: string;
  kind: ShortcutKind;
  /** shell/prompt: the text that is inserted. layout: the layout's name. */
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

// ---- layouts.py -------------------------------------------------------------
// One JSON document per workspace (`.workbench/layouts.json`). The server never
// interprets `state` — it is dockview's `api.toJSON()` output, and the rules
// about which panels may appear in it are a registry fact, so validation lives
// on this side (`ui/src/layouts.ts`).

export interface NamedLayout {
  name: string;
  /** dockview's SerializedDockview. Opaque until `pruneLayout` vets it. */
  state: unknown;
}

export interface LayoutsState {
  /** The live arrangement, restored on the next load. null = never saved. */
  current: unknown;
  /** Which named layout `current` came from — advisory, for the status chip. */
  current_name: string | null;
  saved: NamedLayout[];
}

export interface LayoutsResponse {
  state: LayoutsState;
  /** Non-null when the file on disk could not be used — the UI toasts it and
   * falls back to the default layout. */
  problem: string | null;
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

// ---- office_host.py ---------------------------------------------------------
// A *real* Word/Excel window docked into a panel. Types only for now: the panel
// lands once the tool registry exists and it can register itself.

/** The program that hosts a document. `powerpoint` is a real kind the UI must be
 * able to name, but it is not hostable in v1: PowerPoint is single-instance and
 * offers no way to prove a window is one Workbench launched, so it stays
 * preview-only. The server's `hostable_kinds` (below) is the authority. */
export type HostAppKind = "word" | "excel" | "powerpoint";

/** launching -> embedding -> embedded is the happy path; `detached` is live (the
 * document is open, the window is back on the desktop); closed/crashed/failed
 * are terminal and never move again. */
export type HostState =
  | "closed"
  | "launching"
  | "embedding"
  | "embedded"
  | "detached"
  | "crashed"
  | "failed";

/** Why a host ended up where it is. Only terminal states carry one. */
export type HostReason =
  | "user_closed"
  | "server_shutdown"
  | "launch_timeout"
  | "launch_failed"
  | "embed_refused"
  /** A backend call ran past the server's own ceiling and was cancelled: nobody
   * refused anything, it simply never came back. */
  | "backend_timeout"
  | "process_exited"
  | "document_open_elsewhere"
  | "powerpoint_preview_only"
  | "unsupported_file"
  | "native_hosting_disabled";

/** Physical pixels, relative to the host window. */
export interface PanelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface OfficeHostInfo {
  host_id: string;
  /** Workspace-relative, forward slashes. */
  path: string;
  kind: HostAppKind;
  state: HostState;
  /** null while the host is live. */
  reason: HostReason | null;
  /** The process Workbench launched; null until the launch returns, and never a
   * process it merely found. */
  pid: number | null;
  /** Unix seconds at which the current state was entered. */
  since: number;
  /** The instance was asked to quit and did not. The host is settled either
   * way, but the real window may still be on screen — so "closed" alone would
   * be a claim the server cannot make. Cleared when a later sweep gets the
   * close through, or finds the process gone by itself. */
  close_failed: boolean;
}

/** Broadcast on /ws/events on every host state change. */
export interface OfficeHostEvent {
  type: "office_host";
  host: OfficeHostInfo;
}

/** GET /api/office/hosts — also the reconnect/replay path. In-memory
 * server-side: a restart returns it empty, having reaped every window. */
export interface OfficeHostList {
  hosts: OfficeHostInfo[];
}

/** POST /api/office/host. The application is derived from the extension, so an
 * .xlsx can never be launched into Word. */
export interface OpenHostRequest {
  path: string;
  rect?: PanelRect | null;
}

/** POST /api/office/host/{host_id}/bounds. */
export interface SetBoundsRequest {
  rect: PanelRect;
}

/** POST /api/office/host/{host_id}/visible — the panel went behind another
 * editor tab, or came back. A real window does not hide because a div did. */
export interface SetVisibleRequest {
  visible: boolean;
}

/** What the shell is asked to do with a hosted window: one action per
 * window-facing backend method. Launching, polling and ending the process are
 * the server's own work and never arrive here. */
export type HostCommandAction = "embed" | "set_bounds" | "set_visible" | "detach" | "close";

/** Pushed to the shell over /ws/office-host. Rectangles are in
 * PHYSICAL pixels, like every PanelRect on the wire; the shell divides by its
 * own devicePixelRatio before calling into Rust, which takes CSS pixels and
 * multiplies by the window's scale factor again. */
export interface HostCommand {
  type: "host_command";
  command_id: string;
  host_id: string;
  action: HostCommandAction;
  /** The guest's HWND, for `embed`. */
  window_id: number | null;
  rect: PanelRect | null;
  visible: boolean | null;
}

/** The answer, back up the same socket. `code` is the shell's own refusal code
 * (`embed_refused`, `window_gone`, `unknown_host`, …). */
export interface HostCommandAck {
  type: "host_command_ack";
  command_id: string;
  ok: boolean;
  code: string | null;
  message: string | null;
}

/** GET /api/office/capabilities — what this machine can actually do. The UI
 * degrades from this, never from a guess. */
export interface OfficeCapabilities {
  /** The configured policy. `auto` currently resolves to no native hosting. */
  office_native: "auto" | "on" | "off";
  /** The only field that answers "can I dock a real document right now". */
  native_hosting: boolean;
  office_detected: boolean;
  /** The in-process fake backend is answering: nothing is really hosted. */
  fake_backend: boolean;
  /** The desktop shell is connected to the host channel. A browser tab has no
   * native window to host into, which is why this is reported and not guessed
   * from a user agent. */
  shell_attached: boolean;
  hostable_kinds: HostAppKind[];
  onlyoffice: boolean;
  fallback: "native" | "onlyoffice" | "preview";
  detail: string;
}
