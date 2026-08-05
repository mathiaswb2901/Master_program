/**
 * The anchorable surface of a drawn artifact.
 *
 * One rule decides everything in this file: **the renderer emits the anchor**.
 * A part does not report where it was clicked on screen — it reports which
 * *datum* it draws, as a path into the payload (`../plan/anchors.ts`). So a
 * `<td>` says `["leaf", 2, "row", 14, "col", "Price"]`, and that string is what
 * survives a re-render, a restyle and a theme change, and what the server can
 * check against the artifact before the agent reads it.
 *
 * Cost is deliberate. Outside annotate mode a part with no note renders exactly
 * what it rendered before this file existed — no wrapper, no attribute, no
 * handler — because the render budget (`budget.test.tsx`, 18,000 marks) is a
 * ceiling a per-mark wrapper would eat. A part only becomes an element of its
 * own when it is a target (annotate mode) or when it carries a note.
 *
 * Keyboard parity is not a follow-up: an anchorable part is a real `<button>`
 * (or, inside SVG, a `<g role="button" tabindex=0>` answering Enter and Space),
 * so the mode is fully operable from the keyboard — DESIGN.md §7, where a mode
 * reachable only by mouse is a failure, not a limitation.
 */

import { createContext, useContext, type KeyboardEvent, type ReactNode } from "react";

import { anchorKey, partAnchor } from "../plan/anchors";
import type { AnchorSegment } from "../types";

export interface VisualAnnotation {
  /** The plan node these leaves belong to — every anchor names it. */
  nodeId: string;
  /** `anchorKey` -> note text. Shown in place in **both** modes: a note you
   * cannot see while reading the artifact is a note you will not act on. */
  notes: Record<string, string>;
  /** `anchorKey` of the note whose editor is open, so its part reads as live. */
  editing: string | null;
  /** Non-null only in annotate mode; null leaves parts inert and unfocusable. */
  onPick: ((path: AnchorSegment[]) => void) | null;
}

const AnnotateContext = createContext<VisualAnnotation | null>(null);

export const AnnotateProvider = AnnotateContext.Provider;

export function useAnnotation(): VisualAnnotation | null {
  return useContext(AnnotateContext);
}

/** Everything a part needs to draw itself, or null when it should not exist. */
interface Resolved {
  key: string;
  note: string | undefined;
  live: boolean;
  pick: () => void;
}

function resolve(
  annotation: VisualAnnotation | null,
  path: AnchorSegment[],
): Resolved | null {
  if (annotation === null) return null;
  const key = anchorKey(partAnchor(annotation.nodeId, path));
  const note = annotation.notes[key];
  const onPick = annotation.onPick;
  if (onPick === null && note === undefined) return null;
  return {
    key,
    note,
    live: annotation.editing === key,
    pick: () => {
      onPick?.(path);
    },
  };
}

function classes(base: string, part: Resolved, pickable: boolean): string {
  return [
    base,
    pickable ? "is-target" : "is-inert",
    part.note === undefined ? "" : "is-noted",
    part.live ? "is-editing" : "",
  ]
    .filter((piece) => piece !== "")
    .join(" ");
}

/** The role word and the note, for a reader who cannot see the marker (§7). */
function spoken(label: string, note: string | undefined, pickable: boolean): string {
  const head = pickable ? `Annotate ${label}` : label;
  return note === undefined || note === "" ? head : `${head} — noted: ${note}`;
}

/** The visible half of the pair: a marker, never colour alone. */
function Marker({ note }: { note: string | undefined }) {
  if (note === undefined) return null;
  return (
    <span className="wb-vis-pin" aria-hidden="true">
      ✎
    </span>
  );
}

/**
 * An anchorable piece of HTML — a table cell, a metric, a diff line, a legend
 * key. Renders its children untouched when there is nothing to say about them.
 */
export function Part({
  path,
  label,
  children,
}: {
  path: AnchorSegment[];
  label: string;
  children: ReactNode;
}) {
  const annotation = useAnnotation();
  const part = resolve(annotation, path);
  if (part === null) return <>{children}</>;
  const pickable = annotation?.onPick != null;
  if (!pickable) {
    return (
      <span className={classes("wb-vis-part", part, false)} data-anchor={part.key}>
        {children}
        <Marker note={part.note} />
        <span className="u-sr-only"> {spoken(label, part.note, false)}</span>
      </span>
    );
  }
  return (
    <button
      type="button"
      className={classes("wb-vis-part", part, true)}
      data-anchor={part.key}
      aria-label={spoken(label, part.note, true)}
      onClick={part.pick}
    >
      {children}
      <Marker note={part.note} />
    </button>
  );
}

/**
 * The same, inside SVG, where `<button>` is not an element.
 *
 * A `<g>` with `role="button"` and `tabindex` is focusable and announced; Enter
 * and Space are wired by hand because only a real button gets them free. The
 * marker is a glyph rather than a tint, for the same reason as everywhere else.
 */
export function SvgPart({
  path,
  label,
  className,
  children,
  markerAt,
}: {
  path: AnchorSegment[];
  label: string;
  className: string;
  children: ReactNode;
  /** Where the "has a note" glyph goes, in the drawing's own coordinates. */
  markerAt?: { x: number; y: number };
}) {
  const annotation = useAnnotation();
  const part = resolve(annotation, path);
  if (part === null) return <g className={className}>{children}</g>;
  const pickable = annotation?.onPick != null;
  const onKeyDown = (event: KeyboardEvent<SVGGElement>): void => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    part.pick();
  };
  return (
    <g
      className={classes(className, part, pickable)}
      data-anchor={part.key}
      role={pickable ? "button" : undefined}
      tabIndex={pickable ? 0 : undefined}
      aria-label={spoken(label, part.note, pickable)}
      onClick={pickable ? part.pick : undefined}
      onKeyDown={pickable ? onKeyDown : undefined}
    >
      {children}
      {part.note !== undefined && markerAt !== undefined && (
        <text className="wb-vis-pin-svg" x={markerAt.x} y={markerAt.y} aria-hidden="true">
          ✎
        </text>
      )}
    </g>
  );
}
