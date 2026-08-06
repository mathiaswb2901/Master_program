/** Typed REST client for the workbench server (proxied through Vite at /api). */

import type { Theme } from "./theme";
import type {
  AcknowledgeRequest,
  CallbackResponse,
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
  OfficeLastSave,
  OfficeStatus,
  OkResponse,
  OpenHostRequest,
  PanelRect,
  ProvenanceMap,
  RenameRequest,
  SessionInfo,
  SessionLimits,
  ShortcutsState,
  SwitchWorkspaceRequest,
  TranscriptResponse,
  TreeNode,
  UiState,
  UsageSnapshot,
  WorkspaceState,
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
  const res = await fetch(url, init);
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

export const renameEntry = (body: RenameRequest): Promise<OkResponse> =>
  request("/api/files/rename", jsonInit("POST", body));

export const deleteEntry = (path: string): Promise<OkResponse> =>
  request(`/api/files/content?path=${encodeURIComponent(path)}`, { method: "DELETE" });

export const getShortcuts = (): Promise<ShortcutsState> => request("/api/shortcuts");

export const getProvenance = (): Promise<ProvenanceMap> => request("/api/provenance");

export const getUsage = (): Promise<UsageSnapshot> => request("/api/usage");

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

export const putUiState = (body: UiState): Promise<unknown> =>
  request("/api/agents/ui-state", jsonInit("PUT", body));

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
