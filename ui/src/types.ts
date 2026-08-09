/**
 * Wire types. Mirrors server/src/workbench_server/models/{files,terminal,agents,plans}.py —
 * keep in lockstep with the Pydantic models.
 */

// ---- files.py ---------------------------------------------------------------

/**
 * The whole workspace in one payload — the *search index*, not the file tree.
 * `GET /api/files/tree` is a full recursive walk, so it is fetched on demand
 * (the QuickBar's fuzzy search, the chat's tool-row file links) and never from
 * a watcher event. The tree panel reads `DirListing`, one level at a time.
 */
export interface TreeNode {
  name: string;
  /** Workspace-relative, forward slashes; "" = root. */
  path: string;
  kind: "file" | "dir";
  /** null for files, list (possibly empty) for dirs. */
  children: TreeNode[] | null;
}

/** One visible child of one directory. Childless by design: what is inside a
 * directory is a separate question, asked when it is expanded. */
export interface DirEntry {
  name: string;
  /** Workspace-relative, forward slashes. */
  path: string;
  kind: "file" | "dir";
}

/** `GET /api/files/dir?path=` — exactly one directory listing, never a walk.
 * Directories first, then files, each by lowercased name. */
export interface DirListing {
  /** "" = workspace root. */
  path: string;
  /** The directory's own name; the workspace folder's at the root. */
  name: string;
  entries: DirEntry[];
}

/** Every loaded listing is suspect — re-list what is expanded. Published when
 * the visibility rules moved (a `CACHEDIR.TAG` appeared or went), which no
 * per-file event can describe. */
export interface TreeInvalidatedEvent {
  type: "tree_invalidated";
  reason: "ignore_rules";
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

/** The document kinds the tree can create (M5 item 16). The token *is* the file
 * extension; the UI offers them by name (Word / Excel / …). Mirror of
 * `server/src/workbench_server/models/documents.py`. */
export type DocumentKind = "docx" | "xlsx" | "pptx" | "ipynb" | "py" | "txt" | "md";

export interface CreateDocumentRequest {
  path: string;
  kind: DocumentKind;
}

export interface CreateDocumentResponse {
  path: string;
  hash: string;
}

/** Directories appear here only when added or deleted — the two changes the
 * tree has a row for. */
export interface FileChangedEvent {
  type: "file_changed";
  path: string;
  change: "added" | "modified" | "deleted";
  /** null for deletions, directories, and unreadable/binary-too-large files. */
  hash: string | null;
  /** The path was a directory when the change was observed. False for a
   * deletion, which cannot know — the row goes by path either way. */
  is_dir: boolean;
  origin: "watcher";
}

/** Everything that arrives on /ws/events (see also SessionStatusEvent below). */
export type WorkspaceEvent =
  | FileChangedEvent
  | TreeInvalidatedEvent
  | SessionStatusEvent
  | ShortcutsChangedEvent
  | FileProvenanceEvent
  | OfficeHostEvent
  | WorktreeChangedEvent
  | UsageEvent
  | SessionActivityEvent
  | SessionPermissionEvent
  | OrchestratorEvent
  | ValidationEvent
  | WorkspaceChangedEvent
  | CommandInvokeEvent;

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

// ---- usage.py ---------------------------------------------------------------
// Claude plan limits, as far as the Agent SDK reports them. Every field here is
// one the SDK's `RateLimitInfo` actually has; nothing is derived from a trend.
// The figures only arrive on a live session's stream, so a snapshot is stale
// until you talk to an agent — hence `observed_at` on every bucket and `age_s`
// on the snapshot, and `buckets: []` as the honest "never emitted" state.

/** The SDK's five `RateLimitType` values, plus ours for an event that named no
 * window (`rate_limit_type: null`, which the SDK's own type allows). */
export type UsageBucketKind =
  | "five_hour"
  | "seven_day"
  | "seven_day_opus"
  | "seven_day_sonnet"
  | "overage"
  | "unspecified";

/** `allowed_warning` = approaching the limit; `rejected` = already refused. */
export type UsageStatus = "allowed" | "allowed_warning" | "rejected";

export interface UsageBucket {
  kind: UsageBucketKind;
  status: UsageStatus;
  /** 0..1, or null when the event carried none — never render null as zero. */
  utilization: number | null;
  /** Unix seconds, or null. */
  resets_at: number | null;
  /** Unix seconds this bucket last arrived. Per bucket: windows transition
   * independently, so one figure can be far older than another. */
  observed_at: number;
}

export interface UsageOverage {
  status: UsageStatus;
  resets_at: number | null;
  disabled_reason: string | null;
  observed_at: number;
}

export interface UsageModelCost {
  model: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
}

/** This server's own spend — the degraded view. NOT plan usage. */
export interface UsageSessionCost {
  turns: number;
  total_cost_usd: number;
  last_turn_cost_usd: number | null;
  models: UsageModelCost[];
  observed_at: number | null;
}

/** What one *session* has cost this process. The same arithmetic as
 * `UsageSessionCost`, split by session — and the one authority Mission
 * Control's per-worker budget is enforced against, so the board renders exactly
 * the number the server refuses on. Absent (not zero) until a turn settles. */
export interface UsageSessionEntry {
  session_id: string;
  turns: number;
  cost_usd: number;
  observed_at: number;
}

/** GET /api/usage. In-memory server-side: a restart returns it empty. */
export interface UsageSnapshot {
  /** Fixed presentation order, not arrival order. Empty = never emitted. */
  buckets: UsageBucket[];
  overage: UsageOverage | null;
  observed_at: number | null;
  /** Seconds since `observed_at`, measured on the server so the age does not
   * depend on the browser's clock agreeing with it. */
  age_s: number | null;
  session_cost: UsageSessionCost;
  /** Per-session spend, most expensive first and bounded. */
  sessions: UsageSessionEntry[];
}

/** Broadcast on /ws/events whenever the snapshot changes. */
export interface UsageEvent {
  type: "usage";
  snapshot: UsageSnapshot;
}

// ---- activity.py ------------------------------------------------------------
// What the fleet is touching *right now* — the other end of provenance above.
// That one answers "who wrote this file", after the fact and conservatively;
// this answers "what is happening this second", across every session, whether
// or not this window has that conversation open. Bounded by construction: a
// rolling window per session, a cap on sessions, and everything dropped is
// counted rather than silently lost. Never persisted.

export interface ActivityEntry {
  /** The SDK's `tool_use` id: the same id the chat row settles on. */
  entry_id: string;
  tool: string;
  /** One capped line, built server-side with paths jailed to the workspace. */
  summary: string;
  /** Workspace-relative path this call names, or null when it named none (or
   * named one outside the workspace, which is redacted rather than sent). */
  target: string | null;
  /** Unix seconds. */
  started_at: number;
  /** Unix seconds, or null while the call is still running. */
  settled_at: number | null;
  /** null while running — the distinction the panel is built on, so not a bool. */
  ok: boolean | null;
}

export interface SessionActivity {
  session_id: string;
  folder: string;
  title: string;
  /** What kind of session this is. Carried on the fleet's per-session identity
   * so Mission Control can tell an orchestrator from a chat before it has run a
   * tool — mirrors `SessionActivity.kind` in `models/activity.py`. */
  kind: SessionKind;
  /** Newest first, capped. Ordered by when each call *started*; a settle
   * patches an entry where it stands rather than moving it. */
  entries: ActivityEntry[];
  /** Entries this session's window has dropped. Shown, never silent. */
  dropped: number;
  /** Unix seconds of the most recent thing that happened in this session. */
  active_at: number;
}

/** GET /api/activity. Empty `sessions` = no agent sessions running, which is
 * both the common case and what a restart honestly reports. */
export interface ActivitySnapshot {
  sessions: SessionActivity[];
  max_entries_per_session: number;
  max_sessions: number;
  dropped_sessions: number;
}

/** Broadcast on /ws/events when some sessions' activity changes. Carries whole
 * rows for the sessions that moved (never the fleet, unless the fleet moved)
 * and names the ones that left, because clients hold their own copy. */
export interface SessionActivityEvent {
  type: "session_activity";
  sessions: SessionActivity[];
  removed: string[];
}

// ---- shortcuts.py -----------------------------------------------------------
// One markdown file per scope (workspace + user-global), merged. Nothing an
// entry can do executes: `shell` and `prompt` are INSERTED into a surface, and
// `layout` names one of the user's own saved arrangements and moves panels.
// There is no "run" field by design.

export type ShortcutKind = "shell" | "prompt" | "layout" | "command";
export type ShortcutSource = "workspace" | "global";

export interface ShortcutEntry {
  name: string;
  kind: ShortcutKind;
  /** shell/prompt: the text that is inserted. layout: the layout's name.
   * command: the id of a registered command the UI resolves and runs. */
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

// ---- visuals.py -------------------------------------------------------------
// The scene graph. The model sends structure and numbers; `ui/src/visual/`
// computes every coordinate and emits every pixel. Depth stops at the leaf:
// blocks hold leaves, leaves hold data, nothing nests (see visuals.py for why
// that is a safety and a token-budget property before it is a design one).

/** The whole colour language of a visual — one vocabulary across every leaf. */
export type VisualRole = "neutral" | "accent" | "success" | "warning" | "error";

export interface TableColumn {
  label: string;
  /** `numeric` right-aligns in tabular figures because the *schema* says these
   * are figures (DESIGN.md principle 7) — never because the text looked numeric. */
  type: "text" | "numeric" | "code";
  unit: string;
}

export interface CellHighlight {
  row: number;
  /** null highlights the whole row. */
  column: number | null;
  role: VisualRole;
}

export interface TableLeaf {
  kind: "table";
  title: string;
  columns: TableColumn[];
  rows: string[][];
  highlights: CellHighlight[];
}

export interface ValueAxis {
  kind: "value";
  label: string;
  unit: string;
  scale: "linear" | "log";
}

/** A regular grid in absolute time, labelled in a market's own clock — which is
 * what makes a 23- or 25-hour day draw at its true length (`visual/timeAxis.ts`). */
export interface TimeAxis {
  kind: "time";
  label: string;
  /** ISO-8601 instant, with an offset. */
  start: string;
  step_minutes: number;
  /** IANA zone, validated server-side. */
  timezone: string;
}

export type ChartXAxis = ValueAxis | TimeAxis;

export interface ChartSeries {
  label: string;
  style: "line" | "bar" | "step" | "scatter";
  values: number[];
  /** Empty on a time axis, where the grid supplies x. */
  x: number[];
  axis: "left" | "right";
}

export interface ChartLeaf {
  kind: "chart";
  title: string;
  x: ChartXAxis;
  y: ValueAxis;
  y_right: ValueAxis | null;
  series: ChartSeries[];
}

export interface DiagramNode {
  id: string;
  label: string;
  role: VisualRole;
}

export interface DiagramEdge {
  source: string;
  target: string;
  label: string;
}

/** Acyclic, validated server-side: we lay it out as a layered DAG. */
export interface DiagramLeaf {
  kind: "diagram";
  title: string;
  nodes: DiagramNode[];
  edges: DiagramEdge[];
}

export interface CodeDiffLeaf {
  kind: "code_diff";
  title: string;
  /** A tag for a data attribute — never a loader, never a path. */
  language: string;
  before: string;
  after: string;
}

export interface Metric {
  label: string;
  value: string;
  unit: string;
  role: VisualRole;
}

export interface MetricsLeaf {
  kind: "metrics";
  title: string;
  items: Metric[];
}

export type VisualLeaf = TableLeaf | ChartLeaf | DiagramLeaf | CodeDiffLeaf | MetricsLeaf;

export interface VisualBlock {
  layout: "single" | "row" | "grid" | "split";
  items: VisualLeaf[];
}

export interface VisualNode {
  kind: "visual";
  node_id: string;
  title: string;
  blocks: VisualBlock[];
}

export type PlanNode = OptionGroupNode | StepListNode | QuestionNode | MarkdownNode | VisualNode;

export interface PlanArtifact {
  plan_id: string;
  title: string;
  summary: string;
  /** At most 15 (server-capped). */
  nodes: PlanNode[];
}

/** "no_decision" is timeout/interrupt — never an implied approval. */
export type PlanVerdict = "approve" | "revise" | "reject" | "no_decision";

/** A segment of an anchor path: a key we chose, or an index into the payload's
 * own ordering / a name the payload itself carries. */
export type AnchorSegment = string | number;

/**
 * What a note points at.
 *
 * `path` is a **semantic path the renderer emits**, never a CSS selector:
 * `["leaf", 2, "row", 14, "col", "Price"]` survives a re-render and a restyle
 * because it names data, and the server proves it points into the artifact
 * before the agent ever sees it (`services/plan_anchors.py`). See
 * `ui/src/plan/anchors.ts` for the emitting half.
 */
export interface AnnotationAnchor {
  kind: "plan" | "node" | "part" | "range";
  /** Empty only for `kind: "plan"`. */
  node_id: string;
  /** `(key, value)` pairs; empty for `plan` and `node`. */
  path: AnchorSegment[];
}

export interface PlanAnnotation {
  anchor: AnnotationAnchor;
  text: string;
}

export interface PlanResponse {
  plan_id: string;
  verdict: PlanVerdict;
  /** node_id -> option_id for every option group the user resolved. */
  choices: Record<string, string>;
  /** At most 40 (server-capped) — a part anchor makes one node worth several. */
  annotations: PlanAnnotation[];
  comment: string;
}

// ---- conversations.py -------------------------------------------------------

/** One transcript on disk, as a row in the conversation browser. */
export interface ConversationInfo {
  session_id: string;
  /** Derived by the same function live sessions use — a conversation does not
   * retitle when it stops being live. */
  title: string;
  /** Unix mtime of the transcript: when the conversation was last active. */
  updated_at: number;
  /** User messages carrying text. Tool results are stored as user records and
   * are deliberately not counted. */
  turns: number;
  /** The scan hit its byte budget, so `turns` is a floor — rendered as "120+". */
  turns_capped: boolean;
  /** This transcript was read for its title and turn count. False when it fell
   * outside the response's read budget (`limit`): the conversation exists and
   * is listed — every one always is — but `title` is a placeholder and `turns`
   * is 0 until a wider `limit` reads it. */
  read: boolean;
  /** Local id of the live session continuing this transcript, or null. Opening
   * such a row focuses the pane it is already in instead of forking it. */
  live_session_id: string | null;
  /** Why this row is not fully knowable. Never a reason to drop it. */
  problem: string | null;
}

/** One folder's conversations — a directory of Claude Code's storage. */
export interface ProjectGroup {
  /** The encoded directory name. Always true, never a guess. */
  key: string;
  /** Resolved path when there is one, the encoded key when there is not. */
  label: string;
  resolved: boolean;
  folder: string | null;
  /** Workspace-relative path when the folder is inside this workspace
   * ("" = the workspace root); null when it is outside or unresolved. */
  workspace_relative: string | null;
  /** Whether a row here can be opened now. False is a *visible* refusal. */
  openable: boolean;
  reason: string | null;
  updated_at: number;
  conversations: ConversationInfo[];
}

/** `GET /api/conversations` — read-only view of Claude Code's own storage. */
export interface ConversationStore {
  projects: ProjectGroup[];
  projects_root: string;
  root_exists: boolean;
  total_projects: number;
  /** Every conversation that exists, in every folder — `limit` bounds reading,
   * never listing, so this is also how many rows `projects` holds. */
  total_conversations: number;
  /** How many were read in full (title + turns). Less than
   * `total_conversations` means the rest are listed with `read: false` and are
   * waiting for a wider `limit`. */
  returned_conversations: number;
  limit: number;
  /** When the scan ran. There is no watcher on somebody else's storage. */
  scanned_at: number;
}

// ---- agents.py --------------------------------------------------------------

export type SessionState = "idle" | "working" | "needs_attention";

/** What a session *is*. `chat` is every session Workbench has ever had and is
 * the default everywhere; `orchestrator` carries Mission Control's toolset;
 * `worker` is one an orchestrator spawned, bound to a pooled worktree. A
 * transcript on disk is always `chat` — a kind is live state about a process. */
export type SessionKind = "chat" | "orchestrator" | "worker";

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
  kind: SessionKind;
}

// ---- permissions, fleet-wide ------------------------------------------------
// A permission prompt reaches the human over /ws/agent/{id}, a socket that
// exists only for conversations a window has opened. That is the gap Mission
// Control closes: a blocked agent in a session nobody is looking at is
// invisible until it times out, and with an orchestrator spawning workers the
// session that blocks is by definition one the user never opened.

export interface PendingPermission {
  request_id: string;
  tool: string;
  /** Not redacted, deliberately: an approval you cannot read is one you cannot
   * give informedly. Exactly what the session's own card shows. */
  description: string;
  /** Unix seconds when the agent blocked. The board ages it — a prompt that has
   * waited nine minutes is one minute from being denied by timeout. */
  requested_at: number;
}

export interface SessionPermissions {
  session_id: string;
  title: string;
  folder: string;
  kind: SessionKind;
  pending: PendingPermission[];
}

/** GET /api/agents/permissions — every session blocked on the human. */
export interface PermissionsSnapshot {
  sessions: SessionPermissions[];
}

/** Broadcast on /ws/events when one session's set of open prompts changes.
 * Always the **whole set** for that session, including the empty set: a delta
 * would leave a card standing for a future that is already resolved. */
export interface SessionPermissionEvent {
  type: "session_permission";
  session: SessionPermissions;
}

/** POST /api/agents/sessions/{id}/permission — answering from the board, for a
 * session this window holds no chat socket for. */
export interface PermissionAnswer {
  request_id: string;
  allow: boolean;
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

/** The concurrency ceiling and how close the workspace is to it. The count is
 * sessions *working*, not sessions open — the server's own rule, served rather
 * than guessed here (see `models/agents.py`). */
export interface SessionLimits {
  max_concurrent: number;
  active: number;
}

export interface CreateSessionRequest {
  folder: string;
  resume_session_id?: string | null;
  /** `orchestrator` gives the session Mission Control's toolset. A client may
   * **not** ask for `worker`: a worker is bound to a pooled worktree outside the
   * workspace jail, and only the orchestrator service — which holds the lease —
   * may create one (the server refuses it at the schema). */
  kind?: "chat" | "orchestrator";
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

// ---- office identity (M4) ---------------------------------------------------
// Mirrors server/models/office_host.py: which Microsoft account the machine's
// Office is signed in as, read-only and best-effort. The panel degrades from
// this, never from a guess — `null`/`unknown` where the machine will not say.

/** Whether Office reports itself licensed to edit. `unknown` is a first-class
 * answer, not a failure: the honest report when the machine will not say. The UI
 * degrades on `unknown` exactly as on `unlicensed`, never a fabricated
 * `licensed`. */
export type LicenseState = "licensed" | "unlicensed" | "unknown";

/** One Microsoft account Office has cached. Both halves are best-effort — either
 * can be null — and an account with neither is not reported at all. */
export interface OfficeAccount {
  /** Display name, e.g. "Ada Lovelace"; null when Office cached none. */
  display_name: string | null;
  /** Email / UPN; null when Office cached none. */
  email: string | null;
}

/** GET /api/office/identity — which account the machine's Office is signed in as,
 * and whether it is licensed to edit. Read-only and never guessed. */
export interface OfficeIdentity {
  /** A Microsoft account is signed into Office on this machine. */
  signed_in: boolean;
  /** The account a launched instance would run as, when it can be told apart
   * from the rest; null when nobody is signed in, or several are and the
   * registry alone cannot say which — an honest "cannot tell", never a guess. */
  active: OfficeAccount | null;
  /** Every account Office has cached; empty when none is signed in (read
   * `detail` to tell "none signed in" from "could not read"). */
  accounts: OfficeAccount[];
  license: LicenseState;
  /** One line naming what was found, for the UI and the logs. */
  detail: string;
}

// ---- worktrees.py -----------------------------------------------------------
// The managed worktree pool: borrowed git worktrees so parallel agents get one
// writer per checkout. Slots live OUTSIDE the workspace (under the machine's
// app data dir), which is why `path` here is absolute while every other path on
// this wire is workspace-relative.

/** `free` is the only state a slot can be handed out from. `dirty` holds
 * uncommitted work and is never reclaimed without an explicit override;
 * `needs_review` is the pool refusing to claim a slot it could not put back. */
export type WorktreeState = "free" | "leased" | "dirty" | "needs_review";

/** Who holds a slot, and the two independent reasons they still do: a slot is
 * reclaimable only when the deadline has passed AND the owner process is gone. */
export interface WorktreeLease {
  lease_id: string;
  /** Free text naming the holder — a session id, a task, a worker. */
  holder: string;
  /** null when the caller named no process; the deadline is then the only signal. */
  owner_pid: number | null;
  /** Unix seconds. Renewable — a lease that cannot be renewed is a timeout. */
  expires_at: number;
  acquired_at: number;
  /** Invented while rebuilding the pool from disk after the state file was
   * lost. Nothing is known about who was working here, so the slot is held. */
  recovered: boolean;
}

export interface WorktreeInfo {
  /** Stable id and the directory name under the pool root ("slot-01"). */
  slot: string;
  /** Absolute — a pooled worktree is deliberately outside the workspace. */
  path: string;
  state: WorktreeState;
  /** The detached commit the slot sits on. A pooled worktree has no branch. */
  head: string | null;
  /** Present exactly when state === "leased". */
  lease: WorktreeLease | null;
  /** Paths `git status --porcelain` reported; ignored files (node_modules,
   * .venv, build caches) are not among them, so a warm slot reads as clean. */
  dirty_files: number;
  created_at: number;
  updated_at: number;
  /** One line for a human, on `dirty` and `needs_review`. */
  detail: string | null;
}

/** GET /api/worktrees — the whole pool, capped small enough to send whole. */
export interface WorktreePool {
  root: string;
  /** null when the workspace is not inside a git repository. Not an error. */
  repo: string | null;
  capacity: number;
  slots: WorktreeInfo[];
  /** Why there is no pool, or why it had to be rebuilt from disk. */
  problem: string | null;
}

/** Broadcast on /ws/events whenever a slot changes state. */
export interface WorktreeChangedEvent {
  type: "worktree_changed";
  worktree: WorktreeInfo;
}

// ---- orchestrator.py --------------------------------------------------------
// Mission Control's second half: a session that runs other sessions. The board
// *composes* the activity feed, the usage service and the worktree pool for
// everything else about a session — this is only what none of them can answer:
// who asked for this session, and under what ceiling.

/** The ceilings an orchestrator works under, as the server enforces them.
 * Served rather than restated here: what a ceiling counts is a server rule. */
export interface OrchestratorBudget {
  max_workers: number;
  max_worker_turns: number;
  max_worker_cost_usd: number;
  max_fleet_turns: number;
  max_fleet_cost_usd: number;
}

export type WorkerOutcome = "stopped" | "failed";

export interface WorkerInfo {
  /** The worker session's local id — the same key as its agent pane. */
  worker_id: string;
  orchestrator_id: string;
  task: string;
  /** Pool slot name, or null when there was no pool to borrow from. */
  slot: string | null;
  /** Absolute, and outside the workspace by construction: shown, never opened. */
  path: string;
  /** From the usage service, not a counter of the orchestrator's own. */
  turns: number;
  outcome: WorkerOutcome | null;
  detail: string | null;
  created_at: number;
  updated_at: number;
}

export type SpawnRefusalReason =
  | "worker_limit"
  | "worker_turns"
  | "worker_cost"
  | "fleet_turns"
  | "fleet_cost"
  | "session_limit"
  | "no_worktree"
  | "unavailable";

/** A cap that bound, with everything the pane needs to render it — never a bare
 * message. `setting` is the environment variable that raises the ceiling, which
 * is what keeps a hit cap from being a dead button (DESIGN.md, CLAUDE.md). */
export interface SpawnRefusal {
  reason: SpawnRefusalReason;
  detail: string;
  limit: number | null;
  observed: number | null;
  setting: string | null;
}

export interface OrchestratorInfo {
  orchestrator_id: string;
  workers: WorkerInfo[];
  /** The most recent cap this orchestrator met, so the board shows it where the
   * user is looking rather than only inside a transcript they may not have open. */
  last_refusal: SpawnRefusal | null;
}

/** GET /api/orchestrator — load and reconnect. */
export interface OrchestratorSnapshot {
  orchestrators: OrchestratorInfo[];
  budget: OrchestratorBudget;
  max_concurrent_sessions: number;
  /** null when there is no worktree pool (no git, or not a repository). */
  worktree_capacity: number | null;
}

/** Broadcast on /ws/events whenever an orchestrator's roster changes. */
export interface OrchestratorEvent {
  type: "orchestrator";
  snapshot: OrchestratorSnapshot;
}

export interface AcquireWorktreeRequest {
  holder: string;
  /** Any commit-ish; defaults to the repository's HEAD. */
  base?: string | null;
  owner_pid?: number | null;
  ttl_seconds?: number | null;
}

export interface ReleaseWorktreeRequest {
  lease_id: string;
  /** Throw uncommitted work away instead of parking the slot as `dirty`. */
  discard_changes?: boolean;
}

export interface RenewWorktreeRequest {
  lease_id: string;
  ttl_seconds?: number | null;
}

export interface PruneRequest {
  /** Also reclaim slots holding uncommitted work. Never implied. */
  force?: boolean;
}

/** A slot a sweep deliberately left alone — the half that answers "why is the
 * pool full" without reading a log. */
export interface KeptSlot {
  slot: string;
  reason: "leased" | "owner_alive" | "dirty" | "needs_review" | "reset_failed";
  detail: string | null;
}

export interface PruneResult {
  reclaimed: string[];
  kept: KeptSlot[];
  pool: WorktreePool;
}

// ---- workspaces.py ----------------------------------------------------------
// The workspace is no longer fixed at launch. Paths here are absolute and in the
// OS's own form, because a human reads them and types them back in; everything
// *inside* a workspace still travels as a workspace-relative POSIX path.

export interface WorkspaceRef {
  path: string;
  /** The folder's own name — what a picker row reads as. */
  name: string;
  opened_at: number;
  /** False = not there any more. The row stays, offered as unavailable. */
  exists: boolean;
}

export interface WorkspaceState {
  root: string;
  name: string;
  /** False = nobody chose this root and the server fell back to the directory it
   * was launched from. The first-run case the UI has to *say* rather than
   * present as if it were a decision. */
  explicit: boolean;
  /** Most recent first, current root included. */
  recents: WorkspaceRef[];
  /** Why the recents file was ignored, if it was. */
  problem: string | null;
}

export interface SwitchWorkspaceRequest {
  path: string;
}

/** The root moved under everyone — see `services/workspaces.py`. */
export interface WorkspaceChangedEvent {
  type: "workspace_changed";
  root: string;
  name: string;
}

// ---- auth.py ----------------------------------------------------------------

/** `GET /api/auth/token` — the per-launch token, fetched once at startup and
 * sent on every REST call and WebSocket. Mirrors `AuthTokenResponse`. */
export interface AuthTokenResponse {
  token: string;
}

// ---- sessions.py ------------------------------------------------------------
// Detachable working sessions (M5 item 15). A named session is a manifest that
// ties a workspace + an arrangement + its live agents/leases into one thing a
// user can leave and return to. Held server-side under the machine's app data
// dir, GLOBAL: queried by workspace, never re-rooted by a switch. This is the
// PR2 persistence surface only — no UI wired yet (PR3). `arrangement` is
// dockview's `api.toJSON()` output, opaque here exactly as a layout's `state`.

/** A live agent a session owns, by reference — the SDK session is server-side. */
export interface AgentRef {
  /** dockview panel id of the pane that views this agent. */
  pane_key: string;
  /** The server-side SDK session id the pane is a view onto. */
  sdk_session_id: string;
  /** Workspace-relative folder the agent is bound to ("" = root). */
  folder: string;
}

/** A worktree lease a session holds, by reference — the slot lives in the pool. */
export interface LeaseRef {
  slot: string;
  lease_id: string;
}

export interface NamedSession {
  id: string;
  name: string;
  /** Absolute path of the workspace this session belongs to, OS-native form. */
  workspace: string;
  /** dockview's `api.toJSON()` arrangement. Opaque; null = none captured yet. */
  arrangement: unknown;
  agents: AgentRef[];
  leases: LeaseRef[];
  created_at: number;
  last_attached_at: number;
  /** The goal this session works toward (plan §3), or null. Set/cleared through
   * the dedicated objective endpoints; the whole-manifest PUT carries it over. */
  objective?: Objective | null;
}

/** POST body — the server assigns id/created_at/last_attached_at. Named
 * distinctly from the agent-session `CreateSessionRequest` above: the wire's TS
 * namespace is flat where the server's is per-module. */
export interface CreateNamedSessionRequest {
  name: string;
  workspace: string;
  arrangement?: unknown;
  agents?: AgentRef[];
  leases?: LeaseRef[];
}

/** PUT body — the whole manifest minus the fields the server owns. */
export interface UpdateNamedSessionRequest {
  name: string;
  workspace: string;
  arrangement?: unknown;
  agents?: AgentRef[];
  leases?: LeaseRef[];
}

export interface SessionsResponse {
  sessions: NamedSession[];
  /** Why the sessions file was ignored, if it was. Never fatal. */
  problem: string | null;
}

/** A goal bound to a session (plan §3). A thin record — statement + optional
 * acceptance note; the status is never stored here, it is derived from the
 * validation evidence (`ObjectiveView`). */
export interface Objective {
  statement: string;
  acceptance: string | null;
}

/** An objective's **derived** status. `met` requires a human-approved result
 * (the plan's gate); `at-risk`/`failing` are an unapproved medium / high-or-worse
 * latest result; `open` is the default and a passing-but-unsigned result. */
export type ObjectiveStatus = "open" | "met" | "at-risk" | "failing";

/** GET/PUT/DELETE `/api/sessions/{id}/objective` — the goal, its derived status,
 * and the evidence it came from (the same `ValidationResult` the Review panel
 * renders, so the two badges can never disagree). */
export interface ObjectiveView {
  session_id: string;
  objective: Objective | null;
  status: ObjectiveStatus;
  evidence: ValidationResult | null;
}

/** PUT `/api/sessions/{id}/objective` — bind or re-state the goal. The derived
 * status is never sent; it is computed from the evidence on read. */
export interface SetObjectiveRequest {
  statement: string;
  acceptance?: string | null;
}

// ---- validation.py ----------------------------------------------------------
// Mirrors server/src/workbench_server/models/validation.py. Provenance answers
// "who wrote this file"; validation answers "and is it correct" — one
// ValidationResult, its `risk` derived from the evidence (never asserted), held
// server-side keyed by `validation_id`. No UI panel yet (that is M6 PR3): these
// are the wire types PR3 consumes.

/** The badge value, least-to-most severe. `blocked` is "could not judge" —
 * distinct from a red `high` fail — and a result with no evidence derives it. */
export type RiskLevel = "pass" | "low" | "medium" | "high" | "blocked";

/** One check's verdict on one thing. `skipped` = did not apply / could not
 * evaluate, distinct from `fail` (evaluated, disagreed). */
export type CheckOutcome = "pass" | "warn" | "fail" | "skipped";

/** The kind of evidence, and the key its detail payload is stored under. */
export type EvidenceKind = "numeric" | "gate" | "diff" | "log" | "artifact";

/** AXI shape 1: a capped list says how much was cut and how to widen it. */
export interface EvidenceTruncation {
  shown: number;
  total: number;
  detail: string;
}

/** One line of evidence, with a reference to its detail payload (never the
 * payload inline). `payload_ref` is null when there is no payload behind it. */
export interface EvidenceItem {
  kind: EvidenceKind;
  label: string;
  outcome: CheckOutcome;
  detail: string;
  payload_ref: string | null;
}

/** What was validated: a session's output, a file, or an objective. */
export interface ValidationSubject {
  kind: "session_output" | "file" | "objective";
  /** A session_id, a workspace-relative path, or an objective_id. */
  ref: string;
  label: string;
}

/** The one mandatory human decision on a `medium`-or-worse result. Timestamp is
 * server-minted (ISO 8601 string). */
export interface ValidationApproval {
  approver: string;
  timestamp: string;
  note: string | null;
}

/** One validation's whole answer — the atom the milestone composes with. */
export interface ValidationResult {
  /** Server-minted stable handle; `approve` and GET /{id} address it. */
  validation_id: string;
  subject: ValidationSubject;
  /** Derived from `evidence`, never asserted by the caller. */
  risk: RiskLevel;
  evidence: EvidenceItem[];
  summary: string;
  created_at: string;
  /** null while running. */
  completed_at: string | null;
  /** Set when `evidence` was capped (AXI shape 1); null means whole. */
  truncated: EvidenceTruncation | null;
  /** The human decision once made; null = awaiting approval (medium-or-worse)
   * or not required. */
  approval: ValidationApproval | null;
}

/** POST /api/validation/run — what to validate and with which checks. `checks`
 * empty runs every registered check; `params` is per-check input as data. */
export interface ValidationSpec {
  subject: ValidationSubject;
  checks?: string[];
  params?: Record<string, unknown>;
}

/** POST /api/validation/{id}/approve — the server stamps the timestamp. */
export interface ApproveRequest {
  approver: string;
  note?: string | null;
}

/** GET /api/validation — every result currently held, oldest first. In-memory
 * and bounded; a restart returns an empty list. */
export interface ValidationResults {
  results: ValidationResult[];
}

/** Broadcast on /ws/events when a result is created or approved. Carries the
 * whole result; the client replaces its entry keyed by `validation_id`. */
export interface ValidationEvent {
  type: "validation";
  result: ValidationResult;
}

// ---- search.py --------------------------------------------------------------
// Workspace content search (M7 V7b). `POST /api/search` finds literal text across
// the workspace's files, respecting the file tree's ignore rules (IGNORED_DIRS +
// CACHEDIR.TAG). The result carries the AXI truncation shape: `truncated` says the
// cap was hit, and `max_results` is the argument that widens it. Mirrors
// `models/search.py`.

/** POST /api/search — what to find, and how much to bring back. */
export interface SearchRequest {
  /** Literal text (substring, not a regex); at least one character. */
  query: string;
  /** Cap on hits across all files; the scan stops here and sets `truncated`. */
  max_results?: number;
  /** Default false — case-insensitive is what "find SE3" usually means. */
  case_sensitive?: boolean;
}

/** One matching line in one file. */
export interface SearchHit {
  /** 1-based line number — the line the editor jumps to. */
  line: number;
  /** 0-based character offset of the first match on the line. */
  col: number;
  /** The matching line (trailing newline stripped), clipped when very long. */
  text: string;
  /** True when the line was longer than the clip and `text` is a prefix. */
  line_truncated: boolean;
}

/** Every returned hit in one file, in line order. */
export interface FileMatches {
  /** Workspace-relative, forward slashes — the path the editor opens. */
  path: string;
  hits: SearchHit[];
}

/** POST /api/search response — hits grouped by file. Empty `files` = no matches. */
export interface SearchResponse {
  query: string;
  files: FileMatches[];
  total_hits: number;
  files_with_matches: number;
  /** True when the scan stopped at `max_results` and more matches exist. */
  truncated: boolean;
}

// ---- commands.py ------------------------------------------------------------
// Commands the window does not own (M5 item 14). The window owns the command
// registry; these types are the seam that lets a shell or agent reach one. The
// UI publishes its manifest on connect, the backend validates an id against it,
// and a CommandInvokeEvent on /ws/events carries the request back to the window,
// which runs it and POSTs the result. Mirrors `models/commands.py`.

/** One command the window will run on request. `takes_params` is advisory —
 * today's commands run parameterless. */
export interface CommandManifestItem {
  id: string;
  title: string;
  takes_params: boolean;
}

/** PUT /api/commands/manifest (what the window publishes) and GET /api/commands
 * (what a CLI/agent discovers). Empty until a window has connected. */
export interface CommandManifest {
  commands: CommandManifestItem[];
}

/** Pushed on /ws/events so the connected window runs one command. The window
 * reports back with `invocation_id` so the awaiting caller hears the outcome. */
export interface CommandInvokeEvent {
  type: "command_invoke";
  invocation_id: string;
  command_id: string;
  params: Record<string, unknown>;
}

/** POST /api/commands/result — the window's verdict on one invocation. */
export interface CommandResultRequest {
  invocation_id: string;
  ok: boolean;
  detail: string;
}

// ---- first-run / Setup (M7 §2) ---------------------------------------------
// Mirrors server/models/setup.py. The first-run walkthrough greets a fresh
// workspace and reports what is wired up — Claude login (detected, never
// performed), Office/OnlyOffice readiness (echoed from the office capabilities)
// — then gets out of the way. Every field is server-computed and honest.

/** The connections a first run reports on. */
export type SetupCheckId = "claude_login" | "office" | "onlyoffice" | "workspace" | "shell";

/** What a check found. `action_needed` is the only state that keeps the
 * walkthrough from getting out of the way; `unavailable` is reported honestly
 * (no Office on the machine, a browser tab) but never nags. */
export type SetupCheckState = "ok" | "action_needed" | "unavailable";

/** How a check's one action is carried out: a registered in-app `command`, or an
 * `instruction` the human runs themselves (Claude login is `claude /login`,
 * never a button the app could fire). */
export type SetupActionKind = "command" | "instruction";

/** The one thing that would move a check toward `ok`. Exactly one of
 * `command_id` / `instruction` is set, per `kind`. */
export interface SetupAction {
  kind: SetupActionKind;
  label: string;
  /** A registered command id, for `kind === "command"`. */
  command_id: string | null;
  /** The exact line the human runs, for `kind === "instruction"` — rendered as
   * copyable text, never wired to a handler. */
  instruction: string | null;
}

/** One connection, as the first-run checklist shows it. */
export interface SetupCheck {
  id: SetupCheckId;
  title: string;
  state: SetupCheckState;
  detail: string;
  action: SetupAction | null;
}

/** GET /api/setup/status — the whole first-run picture, computed server-side. */
export interface SetupStatus {
  checks: SetupCheck[];
  /** No `.workbench/` state in this workspace yet — drives the auto-open. */
  first_run: boolean;
  /** Derived: nothing is `action_needed`. The walkthrough (and its status-bar
   * reading) gets out of the way when true. */
  all_ok: boolean;
}

// ---- settings (M7 V8) -------------------------------------------------------
// Mirrors server/models/settings.py. The knobs that used to be environment
// variables, in one small document under the machine's app-data dir — never in
// the workspace and never in `~/.claude`. The stored choices are what the
// controls show; `effective` is what the server is using; the two differ only
// where an override or a pending restart says so.

/** How the window picks its palette. `system` follows the OS preference. */
export type ThemeChoice = "system" | "dark" | "light";

/** The settings a client can address — and the only keys an override names. */
export type SettingKey = "theme" | "office_native" | "voice_input";

/** A user's stored choices, and the PUT body (the same shape). */
export interface WorkbenchSettings {
  theme: ThemeChoice;
  /** Whether documents open in the real installed Word/Excel, docked. Read at
   * launch, so a change is reported in `pending_restart` until a restart. */
  office_native: OfficeCapabilities["office_native"];
  /** Push-to-talk voice input. Remembered even on a machine where the local
   * transcriber is not installed yet (M7 §3). */
  voice_input: boolean;
}

/** A setting this process was configured with from outside the app — an
 * environment variable or `workbench.toml`. The control shows the value and is
 * disabled with `detail` as the reason; the stored choice is left untouched. */
export interface SettingOverride {
  key: SettingKey;
  value: string;
  detail: string;
}

/** Not a setting: the zero-telemetry position, stated by the server. `enabled`
 * is `false` in every shape of this model — there is nothing to turn on. */
export interface TelemetryStance {
  enabled: false;
  detail: string;
}

/** GET/PUT /api/settings — stored choices, what is in force, and why. */
export interface SettingsState {
  stored: WorkbenchSettings;
  effective: WorkbenchSettings;
  overrides: SettingOverride[];
  /** Stored values this running process has not picked up: read at launch only. */
  pending_restart: SettingKey[];
  /** Where the document lives — machine-local state, shown so it is not a mystery. */
  path: string;
  telemetry: TelemetryStance;
  /** Why the stored document was ignored, if it could not be read. */
  problem: string | null;
}
