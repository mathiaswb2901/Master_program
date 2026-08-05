/** In-app confirm modal (never window.confirm): surface-elevated card on the
 * backdrop, --radius-lg + --shadow-3, accent primary action (DESIGN.md §4/§6). */

import { useEffect, useRef } from "react";

import { useStore } from "../store";

export interface ModalAction {
  label: string;
  kind: "primary" | "outline" | "ghost";
  onClick: () => void;
}

export function ConfirmModal({
  title,
  message,
  actions,
  onDismiss,
}: {
  title: string;
  message: string;
  actions: ModalAction[];
  onDismiss: () => void;
}) {
  const primaryRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    primaryRef.current?.focus();
    // Capture phase so Esc wins over Monaco/xterm handlers underneath.
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onDismiss();
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [onDismiss]);

  return (
    <>
      <div className="wb-modal-backdrop" onClick={onDismiss} />
      <div className="wb-modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="wb-modal-title">{title}</div>
        <div className="wb-modal-message">{message}</div>
        <div className="wb-modal-actions">
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              ref={action.kind === "primary" ? primaryRef : undefined}
              className={`wb-btn wb-btn-${action.kind}`}
              onClick={action.onClick}
            >
              {action.label}
            </button>
          ))}
        </div>
      </div>
    </>
  );
}

/** Rendered app-wide: confirms closing a tab whose buffer has unsaved edits. */
export function DirtyCloseModal() {
  const path = useStore((s) => s.pendingClosePath);
  if (path === null) return null;
  const name = path.split("/").pop() ?? path;
  const resolve = (action: "save" | "discard" | "cancel"): void =>
    void useStore.getState().resolvePendingClose(action);
  return (
    <ConfirmModal
      title={`Close ${name}?`}
      message={`${path} has unsaved changes. Save them before closing?`}
      actions={[
        { label: "Save and close", kind: "primary", onClick: () => resolve("save") },
        { label: "Discard changes", kind: "outline", onClick: () => resolve("discard") },
        { label: "Cancel", kind: "ghost", onClick: () => resolve("cancel") },
      ]}
      onDismiss={() => resolve("cancel")}
    />
  );
}

/** The native window close, held while buffers are unsaved. A browser tab gets
 * this from `beforeunload`; WebView2 ignores that, so the Tauri shell routes
 * its `CloseRequested` through the same prompt (see `shell.ts`). One decision
 * covers every dirty buffer — closing the window is not a per-tab act. */
export function ShellCloseModal() {
  const pending = useStore((s) => s.pendingShellClose);
  const openFiles = useStore((s) => s.openFiles);
  if (!pending) return null;
  const dirty = openFiles.filter((f) => f.dirty);
  const noun = dirty.length === 1 ? "file has" : "files have";
  const resolve = (action: "save" | "discard" | "cancel"): void =>
    void useStore.getState().resolveShellClose(action);
  return (
    <ConfirmModal
      title="Close Workbench?"
      message={`${dirty.length} ${noun} unsaved changes: ${dirty
        .map((f) => f.name)
        .join(", ")}.`}
      actions={[
        { label: "Save and close", kind: "primary", onClick: () => resolve("save") },
        { label: "Discard changes", kind: "outline", onClick: () => resolve("discard") },
        { label: "Cancel", kind: "ghost", onClick: () => resolve("cancel") },
      ]}
      onDismiss={() => resolve("cancel")}
    />
  );
}
