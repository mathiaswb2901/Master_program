/**
 * Which overlay answers the keyboard, when more than one is open.
 *
 * Every modal surface in the app listens for its keys the same way — `keydown`
 * on `window`, in the **capture** phase — because that is the only place a key
 * beats Monaco and xterm, which are full keyboard applications that would
 * otherwise swallow Escape. The trouble is that this puts every overlay's
 * listener on the *same target*, and `stopPropagation()` does not touch other
 * listeners on the element it was called from: it stops the event walking
 * further along the tree. Two overlays open at once therefore both handled the
 * same Escape.
 *
 * That is not hypothetical. `App.tsx` mounts the QuickBar and the confirm
 * modals as unconditional siblings, with no mutual exclusion between
 * `quickBarOpen` and `pendingClosePath`/`pendingShellClose`, and a user reaches
 * both orders with one keystroke:
 *
 *  - Palette first: `Ctrl+P`, then `Alt+W` on a dirty buffer. The close
 *    confirmation opens *over* the palette, and one Escape used to close the
 *    palette **and** silently answer "cancel" to the question the user had not
 *    read yet — the dialog on top dismissed as a side effect of dismissing the
 *    one underneath it.
 *  - Confirmation first: click a dirty tab's ×, then `Ctrl+P`. Same key, both
 *    dialogs gone.
 *
 * The native window close is the version with no DOM click in it at all — the
 * shell's `CloseRequested` reaches the store directly, so the palette's
 * full-screen backdrop cannot block it, and the quit prompt lands on top of a
 * palette the user never dismissed.
 *
 * A stack rather than `stopImmediatePropagation()`, and deliberately. Listeners
 * on one target fire in **registration** order, which is the order the overlays
 * *opened*, so an immediate-stop hands the key to the oldest overlay — the one
 * underneath. That is precisely backwards: the topmost dialog owns the
 * keyboard. Here the newest layer is the only one that acts, and every layer
 * below returns without touching the event.
 *
 * A module-level registry rather than store state, on the `terminalInput.ts`
 * pattern (CLAUDE.md): nothing renders from this, no component re-reads it, and
 * it is keyed by an identity each layer holds for its own lifetime. The stack
 * itself is kept free of React and the DOM so the ordering rule — the part that
 * can be wrong — is asserted in `overlays.test.ts` without a browser; the
 * behaviour it buys is `e2e/quickbarA11y.spec.ts`.
 */

import { useEffect, useRef } from "react";

/** Open layers, oldest first. The last one is the one on top. */
const stack: object[] = [];

/**
 * Put a layer on top, and hand back the way to remove it — the shape
 * `terminalInput.ts` uses, so a caller cannot remove a layer it does not own.
 *
 * Removal is **by identity, not a pop**: a layer that closes while another is
 * open above it must leave the one above it on top. Popping would promote a
 * dead layer and hand the keyboard to a dialog that is no longer on screen.
 */
export function pushOverlay(layer: object): () => void {
  stack.push(layer);
  return () => {
    const at = stack.indexOf(layer);
    if (at >= 0) stack.splice(at, 1);
  };
}

/** Does this layer own the keyboard right now? Only the newest one does. */
export function isTopOverlay(layer: object): boolean {
  return stack.length > 0 && stack[stack.length - 1] === layer;
}

/**
 * Handle `keydown` while `active`, but only while this layer is on top.
 *
 * `onKey` is read through a ref, so a handler rebuilt on every render (an inline
 * arrow, which is what a modal's `onDismiss` prop already is) does not
 * re-register the listener. That matters for more than churn: re-registering
 * would remove and re-push the layer, and an overlay that jumped to the top of
 * the stack on an unrelated re-render would steal the keyboard from the dialog
 * genuinely above it.
 */
export function useOverlayKeys(active: boolean, onKey: (event: KeyboardEvent) => void): void {
  const latest = useRef(onKey);
  // Declared before the registration effect below, so on the commit that opens
  // the layer this has already run and the listener sees this render's handler.
  useEffect(() => {
    latest.current = onKey;
  });

  useEffect(() => {
    if (!active) return;
    const layer = {};
    const remove = pushOverlay(layer);
    const onKeyDown = (event: KeyboardEvent): void => {
      // Not the top: say nothing and, above all, consume nothing. The layer that
      // does act stops the event itself, so what is underneath all of this — the
      // editor, the terminal — still never sees the key.
      if (!isTopOverlay(layer)) return;
      latest.current(event);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => {
      window.removeEventListener("keydown", onKeyDown, true);
      remove();
    };
  }, [active]);
}
