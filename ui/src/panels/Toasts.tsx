/** Toast layer: bottom-right stack on --surface-overlay with a semantic left
 * edge; auto-dismissed by the store after ~6 s, dismissable by hand.
 *
 * A toast that vanished the instant the store dropped it never read as
 * dismissed — it read as a glitch. `useLeaving` keeps each one mounted for
 * `--motion-exit-ms` while it slides out (DESIGN.md §5); the store stays the
 * single owner of which toasts exist, and knows nothing about the animation. */

import { useLeaving } from "../motion";
import { useStore, type Toast } from "../store";

/** Stable across renders — `useLeaving` takes it as a dependency. */
const toastKey = (toast: Toast): string => String(toast.id);

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const shown = useLeaving(toasts, toastKey);
  if (shown.length === 0) return null;
  return (
    <div className="wb-toasts" role="region" aria-label="Notifications">
      {shown.map(({ item: toast, leaving }) => (
        <div
          key={toast.id}
          className={`wb-toast is-${toast.kind}` + (leaving ? " is-leaving" : "")}
          role="status"
        >
          <span className="wb-toast-msg">{toast.message}</span>
          <button
            type="button"
            className="wb-toast-close"
            aria-label="Dismiss notification"
            tabIndex={leaving ? -1 : undefined}
            onClick={() => useStore.getState().dismissToast(toast.id)}
          >
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
