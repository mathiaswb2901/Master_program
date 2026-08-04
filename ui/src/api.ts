/** Typed REST client for the workbench server (proxied through Vite at /api). */

import type {
  CreateSessionRequest,
  FileContent,
  FolderSessions,
  SessionInfo,
  TranscriptResponse,
  TreeNode,
  UiState,
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

export const getTree = (): Promise<TreeNode> => request("/api/files/tree");

export const getFileContent = (path: string): Promise<FileContent> =>
  request(`/api/files/content?path=${encodeURIComponent(path)}`);

export const putFileContent = (body: WriteRequest): Promise<WriteResponse> =>
  request("/api/files/content", jsonInit("PUT", body));

export const getSessions = (): Promise<FolderSessions[]> => request("/api/agents/sessions");

export const createSession = (body: CreateSessionRequest): Promise<SessionInfo> =>
  request("/api/agents/sessions", jsonInit("POST", body));

export const getTranscript = (folder: string, sessionId: string): Promise<TranscriptResponse> =>
  request(
    `/api/agents/transcript?folder=${encodeURIComponent(folder)}&session_id=${encodeURIComponent(sessionId)}`,
  );

export const putUiState = (body: UiState): Promise<unknown> =>
  request("/api/agents/ui-state", jsonInit("PUT", body));
