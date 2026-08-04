/** Toast layer: bottom-right stack on --surface-overlay with a semantic left
 * edge; auto-dismissed by the store after ~6 s, dismissable by hand. */

import { useStore } from "../store";

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  if (toasts.length === 0) return null;
  return (
    <div className="wb-toasts" role="region" aria-label="Notifications">
      {toasts.map((toast) => (
        <div key={toast.id} className={`wb-toast is-${toast.kind}`} role="status">
          <span className="wb-toast-msg">{toast.message}</span>
          <button
            type="button"
            className="wb-toast-close"
            aria-label="Dismiss notification"
            onClick={() => useStore.getState().dismissToast(toast.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
