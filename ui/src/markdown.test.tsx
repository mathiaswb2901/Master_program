import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { Markdown } from "./markdown";

const html = (text: string): string => renderToStaticMarkup(<Markdown text={text} />);

describe("markdown renderer — injection safety", () => {
  it("escapes raw HTML instead of rendering it", () => {
    const out = html("<script>alert(1)</script>");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("escapes event-handler attributes", () => {
    const out = html('<img src=x onerror="alert(1)">');
    expect(out).not.toContain("<img");
    expect(out).not.toContain("onerror=\"alert(1)\"");
  });

  it("refuses javascript: links, keeping the literal text", () => {
    const out = html("[click](javascript:alert)");
    expect(out).not.toContain("href");
    expect(out).toContain("javascript:alert");
  });

  it("refuses data: links", () => {
    const out = html("[x](data:text/html;base64,PHN2Zz4=)");
    expect(out).not.toContain("href");
  });

  it("renders http(s) links with a safe rel", () => {
    const out = html("[docs](https://example.com/a)");
    expect(out).toContain('href="https://example.com/a"');
    expect(out).toContain('rel="noopener noreferrer"');
  });

  it("keeps HTML inside code fences as text", () => {
    const out = html("```\n<script>alert(1)</script>\n```");
    expect(out).toContain("<pre>");
    expect(out).not.toContain("<script>");
  });
});

describe("markdown renderer — blocks", () => {
  it("renders headings, lists and inline code", () => {
    const out = html("# Title\n\n- one\n- two\n\nUse `npm run test`.");
    expect(out).toContain("<h1>Title</h1>");
    expect(out).toContain("<li>one</li>");
    expect(out).toContain("<code>npm run test</code>");
  });

  it("renders an unterminated fence while streaming", () => {
    const out = html("```python\nx = 1");
    expect(out).toContain("x = 1");
  });
});
