/**
 * Minimal hand-rolled markdown renderer for chat messages (DESIGN.md §6.3).
 *
 * Supported: headings, bold/italic, inline code, fenced code blocks, links,
 * ordered/unordered lists, paragraphs. Everything is built as React elements —
 * source text only ever becomes text nodes, so there is no HTML injection
 * surface and no dangerouslySetInnerHTML anywhere.
 */

import type { ReactNode } from "react";

/** Only protocols that cannot execute script in our origin. */
const SAFE_LINK = /^(https?:|mailto:)/i;

// Earliest-match-wins inline tokenizer. Order in the alternation resolves
// overlaps at the same index: code spans > bold > italic > links.
const INLINE = new RegExp(
  "(`[^`\\n]+`)" + // 1 code span
    "|(\\*\\*[^*\\n]+\\*\\*)" + // 2 bold **
    "|(__[^_\\n]+__)" + // 3 bold __
    "|(\\*[^*\\n]+\\*)" + // 4 italic *
    "|(_[^_\\n]+_)" + // 5 italic _
    "|(\\[[^\\]\\n]*\\]\\([^()\\s]+\\))", // 6 link [text](url)
  "g",
);

const LINK = /^\[([^\]]*)\]\(([^()\s]+)\)$/;

function renderInline(text: string, depth = 0): ReactNode[] {
  if (depth > 4) return [text];
  const out: ReactNode[] = [];
  let cursor = 0;
  let key = 0;
  // matchAll clones the regex — recursion below can't clobber iteration state.
  for (const m of text.matchAll(INLINE)) {
    const index = m.index ?? 0;
    if (index > cursor) out.push(text.slice(cursor, index));
    const token = m[0];
    const k = `i${depth}-${key++}`;
    if (token.startsWith("`")) {
      out.push(<code key={k}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith("**") || token.startsWith("__")) {
      out.push(<strong key={k}>{renderInline(token.slice(2, -2), depth + 1)}</strong>);
    } else if (token.startsWith("*") || token.startsWith("_")) {
      out.push(<em key={k}>{renderInline(token.slice(1, -1), depth + 1)}</em>);
    } else {
      const link = LINK.exec(token);
      if (link !== null && SAFE_LINK.test(link[2])) {
        out.push(
          <a key={k} href={link[2]} target="_blank" rel="noopener noreferrer">
            {renderInline(link[1], depth + 1)}
          </a>,
        );
      } else {
        out.push(token); // unsafe or malformed link — keep the literal text
      }
    }
    cursor = index + token.length;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

const HEADING = /^(#{1,6})\s+(.*)$/;
const UL_ITEM = /^\s{0,3}[-*+]\s+(.*)$/;
const OL_ITEM = /^\s{0,3}\d{1,9}[.)]\s+(.*)$/;
const FENCE = /^\s{0,3}(```|~~~)\s*(\S*)\s*$/;

function renderBlocks(text: string): ReactNode[] {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;

  const flushParagraph = (para: string[]): void => {
    const joined = para.join("\n").trim();
    if (joined !== "") out.push(<p key={`b${key++}`}>{renderInline(joined)}</p>);
  };

  while (i < lines.length) {
    const line = lines[i];

    const fence = FENCE.exec(line);
    if (fence !== null) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1; // closing fence (or end of input while streaming)
      out.push(
        <pre key={`b${key++}`} data-lang={fence[2] || undefined}>
          <code>{body.join("\n")}</code>
        </pre>,
      );
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading !== null) {
      const Tag = `h${heading[1].length}` as "h1" | "h2" | "h3" | "h4" | "h5" | "h6";
      out.push(<Tag key={`b${key++}`}>{renderInline(heading[2])}</Tag>);
      i += 1;
      continue;
    }

    const listMatch = (l: string): RegExpExecArray | null => UL_ITEM.exec(l) ?? OL_ITEM.exec(l);
    if (listMatch(line) !== null) {
      const ordered = OL_ITEM.test(line);
      const test = ordered ? OL_ITEM : UL_ITEM;
      const items: string[] = [];
      while (i < lines.length) {
        const m = test.exec(lines[i]);
        if (m === null) break;
        items.push(m[1]);
        i += 1;
      }
      const children = items.map((item, n) => <li key={n}>{renderInline(item)}</li>);
      out.push(
        ordered ? <ol key={`b${key++}`}>{children}</ol> : <ul key={`b${key++}`}>{children}</ul>,
      );
      continue;
    }

    if (line.trim() === "") {
      i += 1;
      continue;
    }

    // Paragraph: accumulate until a blank line or another block form.
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !FENCE.test(lines[i]) &&
      !HEADING.test(lines[i]) &&
      listMatch(lines[i]) === null
    ) {
      para.push(lines[i]);
      i += 1;
    }
    flushParagraph(para);
  }
  return out;
}

export function Markdown({ text }: { text: string }) {
  return <div className="wb-md">{renderBlocks(text)}</div>;
}
