/**
 * Office document panel: hosts an OnlyOffice editor for the active office file,
 * or a calm degraded-mode card when no Document Server is configured. Paper
 * doctrine per DESIGN.md §2.8/§6.1 — the panel body is the paper surround and
 * every state card renders on paper colors.
 */

import { useEffect, useRef, useState, type ReactNode } from "react";

import * as api from "../api";
import { loadDocsApi, type DocEditorInstance } from "../office";
import { useStore, type OpenFile } from "../store";

let mountCounter = 0;

function StateCard({
  title,
  file,
  hint,
  children,
}: {
  title: string;
  file: OpenFile;
  hint: string;
  children?: ReactNode;
}) {
  return (
    <div className="wb-office-state">
      <div className="wb-office-card">
        <div className="wb-office-card-title">{title}</div>
        <div className="wb-office-card-file u-truncate" title={file.path}>
          {file.name}
        </div>
        <div className="wb-office-card-hint">{hint}</div>
        {children}
      </div>
    </div>
  );
}

function DegradedCard({ file }: { file: OpenFile }) {
  const [copied, setCopied] = useState(false);
  const copyPath = async (): Promise<void> => {
    try {
      await navigator.clipboard.writeText(file.path);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard unavailable — nothing sensible to do
    }
  };
  return (
    <StateCard
      title="Office editing not configured"
      file={file}
      hint="Install OnlyOffice Docs and point the server at it — see the Office editing setup in the README. Until then, open this file in your usual Office app."
    >
      <button type="button" className="wb-office-btn" onClick={() => void copyPath()}>
        {copied ? "Copied" : "Copy full path"}
      </button>
    </StateCard>
  );
}

export function OfficePanel({ file }: { file: OpenFile }) {
  const status = useStore((s) => s.officeStatus);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);

  // Cached in the store after the first fetch; retried here if that failed.
  useEffect(() => {
    void useStore.getState().ensureOfficeStatus();
  }, []);

  const serverUrl = status !== null && status.enabled ? status.server_url : null;

  useEffect(() => {
    if (serverUrl === null) return;
    const host = hostRef.current;
    if (host === null) return;
    let cancelled = false;
    let editor: DocEditorInstance | null = null;
    // DocsAPI takes an element id and replaces the element — give it a fresh
    // imperative mount node so React never fights over the subtree.
    const mount = document.createElement("div");
    mount.id = `wb-office-mount-${String(++mountCounter)}`;
    host.replaceChildren(mount);
    setError(null);
    setReady(false);
    void (async () => {
      const docsApi = await loadDocsApi(serverUrl);
      const config = await api.getOfficeConfig(file.path);
      if (cancelled) return;
      editor = new docsApi.DocEditor(mount.id, { ...config, height: "100%", width: "100%" });
      setReady(true);
    })().catch((err: unknown) => {
      if (cancelled) return;
      setError(
        err instanceof api.ApiError
          ? err.detail
          : err instanceof Error
            ? err.message
            : String(err),
      );
    });
    return () => {
      cancelled = true;
      editor?.destroyEditor();
      host.replaceChildren();
    };
  }, [file.path, file.officeGeneration, serverUrl]);

  if (status === null) {
    return (
      <div className="wb-office">
        <StateCard title="Opening document" file={file} hint="Checking Office configuration…" />
      </div>
    );
  }

  if (serverUrl === null) {
    return (
      <div className="wb-office">
        <DegradedCard file={file} />
      </div>
    );
  }

  return (
    <div className="wb-office">
      {file.conflict !== null && (
        <div className="wb-office-bar" role="alert">
          <span className="wb-office-bar-msg u-truncate">{file.conflict}</span>
          <button
            type="button"
            className="wb-btn wb-btn-sm wb-btn-outline"
            onClick={() => useStore.getState().reopenOffice(file.path)}
          >
            Reopen
          </button>
        </div>
      )}
      <div className="wb-office-body">
        <div ref={hostRef} className="wb-office-frame" />
        {error !== null ? (
          <StateCard title="Could not open document" file={file} hint={error}>
            <button
              type="button"
              className="wb-office-btn"
              onClick={() => useStore.getState().reopenOffice(file.path)}
            >
              Try again
            </button>
          </StateCard>
        ) : (
          !ready && <StateCard title="Opening document" file={file} hint="Loading the editor…" />
        )}
        {file.saveAck && (
          <div className="wb-office-saved" role="status">
            <span className="wb-office-saved-dot" aria-hidden="true" />
            Saved
          </div>
        )}
      </div>
    </div>
  );
}
