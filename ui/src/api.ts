/** Typed REST client for the workbench server (proxied through Vite at /api). */

import { apiUrl } from "./backend";
import type { Theme } from "./theme";
import { getToken } from "./token";
import type {
  AcknowledgeRequest,
  ActivitySnapshot,
  CallbackResponse,
  CommandManifest,
  CommandResultRequest,
  ConversationStore,
  CreateDocumentRequest,
  CreateDocumentResponse,
  CreateNamedSessionRequest,
  CreateRequest,
  CreateSessionRequest,
  DirListing,
  DocEditorConfig,
  FileContent,
  FolderSessions,
  LayoutsResponse,
  LayoutsState,
  OfficeCapabilities,
  OfficeHostInfo,
  OfficeHostList,
  OfficeIdentity,
  OfficeLastSave,
  OfficeStatus,
  OkResponse,
  OpenHostRequest,
  OrchestratorSnapshot,
  PanelRect,
  PermissionAnswer,
  PermissionsSnapshot,
  NamedSession,
  ProvenanceMap,
  RenameRequest,
  SessionInfo,
  SessionLimits,
  SessionsResponse,
  ShortcutsState,
  SwitchWorkspaceRequest,
  TranscriptResponse,
  TreeNode,
  UiState,
  UsageSnapshot,
  WorkspaceState,
  WorktreePool,
  WriteRequest,
  WriteResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(detail);
    this.name = "ApiError";
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  // The per-launch token rides a header on every REST call — one merge here
  // covers all of them. Enforcement is off server-side today, so a null token
  // (handshake not yet resolved) changes nothing; PR4 flips enforcement on.
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token !== null) headers.set("X-Workbench-Token", token);
  const res = await fetch(apiUrl(url), { ...init, headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body — keep the status line
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

/** Like `request`, but for an endpoint that answers 204 No Content — parsing an
 * empty body as JSON would throw. The token header and error handling are the
 * same; only the success path differs. */
async function requestVoid(url: string, init?: RequestInit): Promise<void> {
  const token = getToken();
  const headers = new Headers(init?.headers);
  if (token !== null) headers.set("X-Workbench-Token", token);
  const res = await fetch(apiUrl(url), { ...init, headers });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: string };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // non-JSON error body — keep the status line
    }
    throw new ApiError(res.status, detail);
  }
}

const jsonInit = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

/** One directory's children — what the file tree is built from. `""` = root. */
export const getDir = (path: string): Promise<DirListing> =>
  request(`/api/files/dir?path=${encodeURIComponent(path)}`);

/** The whole workspace in one walk. The QuickBar's search index, fetched on
 * demand — never from a watcher event. */
export const getTree = (): Promise<TreeNode> => request("/api/files/tree");

export const getFileContent = (path: string): Promise<FileContent> =>
  request(`/api/files/content?path=${encodeURIComponent(path)}`);

export const putFileContent = (body: WriteRequest): Promise<WriteResponse> =>
  request("/api/files/content", jsonInit("PUT", body));

export const createEntry = (body: CreateRequest): Promise<OkResponse> =>
  request("/api/files/create", jsonInit("POST", body));

/** Create a valid blank document of a named kind — not a zero-byte file. */
export const createDocument = (body: CreateDocumentRequest): Promise<CreateDocumentResponse> =>
  request("/api/files/document", jsonInit("POST", body));

export const renameEntry = (body: RenameRequest): Promise<OkResponse> =>
  request("/api/files/rename", jsonInit("POST", body));

export const deleteEntry = (path: string): Promise<OkResponse> =>
  request(`/api/files/content?path=${encodeURIComponent(path)}`, { method: "DELETE" });

export const getShortcuts = (): Promise<ShortcutsState> => request("/api/shortcuts");

export const getProvenance = (): Promise<ProvenanceMap> => request("/api/provenance");

export const getUsage = (): Promise<UsageSnapshot> => request("/api/usage");

export const getActivity = (): Promise<ActivitySnapshot> => request("/api/activity");

export const getOrchestrators = (): Promise<OrchestratorSnapshot> => request("/api/orchestrator");

/** The worktree pool. First UI reader: Mission Control, which shows which slot
 * each worker holds (the pool shipped backend-only in #46). */
export const getWorktrees = (): Promise<WorktreePool> => request("/api/worktrees");

export const stopOrchestrator = (id: string): Promise<OrchestratorSnapshot> =>
  request(`/api/orchestrator/sessions/${encodeURIComponent(id)}/stop`, { method: "POST" });

export const getPendingPermissions = (): Promise<PermissionsSnapshot> =>
  request("/api/agents/permissions");

/** Answer a permission prompt without holding that session's chat socket — the
 * whole point of Mission Control's inline chips. */
export const answerPermission = (
  sessionId: string,
  body: PermissionAnswer,
): Promise<SessionInfo> =>
  request(
    `/api/agents/sessions/${encodeURIComponent(sessionId)}/permission`,
    jsonInit("POST", body),
  );

export const getLayouts = (): Promise<LayoutsResponse> => request("/api/layouts");

export const getWorkspace = (): Promise<WorkspaceState> => request("/api/workspace");

/** Re-root the server. 400 with the reason for a folder that cannot be served. */
export const switchWorkspace = (body: SwitchWorkspaceRequest): Promise<WorkspaceState> =>
  request("/api/workspace/switch", jsonInit("POST", body));

export const putLayouts = (body: LayoutsState): Promise<OkResponse> =>
  request("/api/layouts", jsonInit("PUT", body));

export const acknowledgeProvenance = (body: AcknowledgeRequest): Promise<OkResponse> =>
  request("/api/provenance/acknowledge", jsonInit("POST", body));

export const getSessions = (): Promise<FolderSessions[]> => request("/api/agents/sessions");

export const getSessionLimits = (): Promise<SessionLimits> => request("/api/agents/limits");

export const createSession = (body: CreateSessionRequest): Promise<SessionInfo> =>
  request("/api/agents/sessions", jsonInit("POST", body));

export const getTranscript = (folder: string, sessionId: string): Promise<TranscriptResponse> =>
  request(
    `/api/agents/transcript?folder=${encodeURIComponent(folder)}&session_id=${encodeURIComponent(sessionId)}`,
  );

/** A LIVE session's transcript, keyed by its local id — the union of every SDK
 * transcript it has lived under. Used to rehydrate a chat a reload emptied, so a
 * reattached pane shows the conversation the socket only carries forward. */
export const getLiveTranscript = (localId: string): Promise<TranscriptResponse> =>
  request(`/api/agents/${encodeURIComponent(localId)}/transcript`);

export const putUiState = (body: UiState): Promise<unknown> =>
  request("/api/agents/ui-state", jsonInit("PUT", body));

/** The named-session store (M5 item 15). A detached agent session is recorded
 * here as a one-agent manifest so it survives having no pane and can be
 * reattached from the Detached list. `workspace` filters to this project's
 * sessions — the store is global and queried by workspace, never re-rooted. */
export const listNamedSessions = (workspace: string): Promise<SessionsResponse> =>
  request(`/api/sessions?workspace=${encodeURIComponent(workspace)}`);

export const createNamedSession = (body: CreateNamedSessionRequest): Promise<NamedSession> =>
  request("/api/sessions", jsonInit("POST", body));

export const deleteNamedSession = (id: string): Promise<void> =>
  requestVoid(`/api/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });

/** Every Claude conversation on this machine, grouped by the folder it ran in.
 * `limit` bounds only the expensive half — everything that exists is counted,
 * and the newest `limit` are read for their title and turn count. */
export const getConversations = (limit?: number): Promise<ConversationStore> =>
  request(
    limit === undefined ? "/api/conversations" : `/api/conversations?limit=${String(limit)}`,
  );

// ---- command relay (M5 item 14) ---------------------------------------------
// The window publishes what it will run, and reports how each invocation went.
// The invoke half lives outside the window (the CLI and the `run_command` agent
// tool); the window is only the executor here.

/** Publish the manifest of commands this window will run on request. Called on
 * every (re)connect so the backend always validates against the live window. */
export const publishCommandManifest = (body: CommandManifest): Promise<OkResponse> =>
  request("/api/commands/manifest", jsonInit("PUT", body));

/** Report how one relayed command turned out, completing the awaiting invoke. */
export const reportCommandResult = (body: CommandResultRequest): Promise<OkResponse> =>
  request("/api/commands/result", jsonInit("POST", body));

export const getOfficeStatus = (): Promise<OfficeStatus> => request("/api/office/status");

export const getOfficeConfig = (path: string, theme: Theme): Promise<DocEditorConfig> =>
  request(`/api/office/config?path=${encodeURIComponent(path)}&theme=${theme}`);

export const postOfficeForcesave = (path: string): Promise<CallbackResponse> =>
  request("/api/office/forcesave", jsonInit("POST", { path }));

// ---- native Office host (M4) -------------------------------------------------
// The panel degrades from `capabilities` and never from a guess: it is the one
// answer to "can a real document be docked in this window right now".

export const getOfficeCapabilities = (): Promise<OfficeCapabilities> =>
  request("/api/office/capabilities");

/** Which Microsoft account this machine's Office is signed in as, and whether it
 * is licensed to edit. Read-only and best-effort; the panel degrades from it. */
export const getOfficeIdentity = (): Promise<OfficeIdentity> =>
  request("/api/office/identity");

export const openOfficeHost = (body: OpenHostRequest): Promise<OfficeHostInfo> =>
  request("/api/office/host", jsonInit("POST", body));

export const getOfficeHosts = (): Promise<OfficeHostList> => request("/api/office/hosts");

export const setOfficeHostBounds = (hostId: string, rect: PanelRect): Promise<OfficeHostInfo> =>
  request(`/api/office/host/${encodeURIComponent(hostId)}/bounds`, jsonInit("POST", { rect }));

export const setOfficeHostVisible = (hostId: string, visible: boolean): Promise<OfficeHostInfo> =>
  request(`/api/office/host/${encodeURIComponent(hostId)}/visible`, jsonInit("POST", { visible }));

export const detachOfficeHost = (hostId: string): Promise<OfficeHostInfo> =>
  request(`/api/office/host/${encodeURIComponent(hostId)}/detach`, { method: "POST" });

export const closeOfficeHost = (hostId: string): Promise<OfficeHostInfo> =>
  request(`/api/office/host/${encodeURIComponent(hostId)}/close`, { method: "POST" });

/** Defensive: the endpoint may not exist yet — 404 means "never saved" (null). */
export const getOfficeLastSave = async (path: string): Promise<OfficeLastSave> => {
  try {
    return await request(`/api/office/last-save?path=${encodeURIComponent(path)}`);
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) return { hash: null };
    throw err;
  }
};
