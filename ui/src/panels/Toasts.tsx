/** Toast layer: bottom-right stack on --surface-overlay with a semantic left
 * edge; auto-dismissed by the store after ~6 s, dismissable by hand.
 *
 * A toast that vanished the instant the store dropped it never read as
 * dismissed — it read as a glitch. `useLeaving` keeps each one mounted for
 * `--motion-exit-ms` while it slides out (DESIGN.md §5); the store stays the
 * single owner of which toasts exist, and knows nothing about the animation.
 *
 * ANVIL V4 gave each kind a **glyph and a name** beside its coloured edge.
 * DESIGN.md §7 forbids colour as the only signal, and an edge tint was exactly
 * that: a red rule and an orange rule are one hue apart at 2px, and to a screen
 * reader they were nothing at all. The glyph is the sighted signal, the name is
 * the spoken one, and an error is announced assertively rather than politely —
 * the one toast the user must not miss because they were reading elsewhere.
 * Inline SVG rather than an icon dependency: four 14px marks, on the launch
 * path, in a module the entry imports statically (`e2e/perf/bundle.spec.ts`).
 */

import { useLeaving } from "../motion";
import { useStore, type Toast, type ToastKind } from "../store";

/** Stable across renders — `useLeaving` takes it as a dependency. */
const toastKey = (toast: Toast): string => String(toast.id);

/** What a screen reader says before the message. Never rendered visually — the
 * glyph is what a sighted reader gets, and a shouted "ERROR" label above a
 * one-line message is the opposite of this bar's voice. */
const KIND_LABEL: Record<ToastKind, string> = {
  error: "Error",
  warn: "Warning",
  info: "Information",
  success: "Success",
};

/** Lucide-shaped 14px marks, 1.5px stroke, drawn in `currentColor` so the
 * `.wb-toast-mark` rule alone decides the hue per kind. */
function KindMark({ kind }: { kind: ToastKind }) {
  return (
    <svg
      className="wb-toast-mark"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {kind === "success" ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M8 12.5l2.5 2.5L16 9.5" />
        </>
      ) : kind === "error" ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M15 9l-6 6M9 9l6 6" />
        </>
      ) : kind === "warn" ? (
        <>
          <path d="M12 4.5L21 20H3z" />
          <path d="M12 10v4.5M12 17.2v.3" />
        </>
      ) : (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="M12 16v-4.5M12 8.2v.3" />
        </>
      )}
    </svg>
  );
}

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
          role={toast.kind === "error" ? "alert" : "status"}
        >
          <KindMark kind={toast.kind} />
          <span className="u-sr-only">{KIND_LABEL[toast.kind]}</span>
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
