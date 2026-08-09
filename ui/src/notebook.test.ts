/**
 * The notebook module's pure half: what counts as a notebook, and the token
 * pass that colours a code cell.
 *
 * The highlighter is the part worth pinning. It is hand-rolled — the same call
 * `markdown.tsx` made for chat, and for the same reason: Monaco is 3.3 MB
 * behind a dynamic import and `colorize` returns an HTML string. So the
 * property that makes it safe to use on a user's file is asserted directly:
 * **concatenating the tokens reproduces the source exactly**. A highlighter
 * that drops a character is a viewer that lies about the file, and no visual
 * check would ever catch one.
 */

import { describe, expect, it } from "vitest";

import {
  byteSize,
  countPhrase,
  executionLabel,
  isNotebookPath,
  notebookTitle,
  tokenizeCode,
  type CodeToken,
} from "./notebook";

const joined = (tokens: readonly CodeToken[]): string => tokens.map((t) => t.text).join("");
const kinds = (source: string, language = "python"): string[] =>
  tokenizeCode(source, language).map((t) => t.kind);
const textOf = (source: string, kind: string, language = "python"): string[] =>
  tokenizeCode(source, language)
    .filter((t) => t.kind === kind)
    .map((t) => t.text);

describe("isNotebookPath", () => {
  it.each([
    ["analysis.ipynb", true],
    ["se3/deep/model.ipynb", true],
    ["SHOUTED.IPYNB", true],
    ["notes.md", false],
    ["ipynb", false],
    [".ipynb", false], // a dotfile called `.ipynb` has no name, only an extension
    ["archive.ipynb.bak", false],
    ["", false],
  ])("%s -> %s", (path, expected) => {
    expect(isNotebookPath(path)).toBe(expected);
  });
});

describe("notebookTitle", () => {
  it("is the file name, which is what the user calls it", () => {
    expect(notebookTitle("se3/prices/spread.ipynb")).toBe("spread.ipynb");
    expect(notebookTitle("top.ipynb")).toBe("top.ipynb");
  });
});

describe("tokenizeCode", () => {
  it("reproduces the source exactly, whatever it holds", () => {
    const sources = [
      "",
      "x = 1",
      "# just a comment",
      'print("hello")  # trailing',
      '"""A docstring\nover lines."""\nimport pandas as pd\n',
      "df = df[df.price > 42.5e-3]\n\n@decorator\ndef f(a, b):\n    return a if a else b\n",
      "s = 'it\\'s escaped' + \"and \\\"this\\\"\"\n",
      "unicode = 'öre/kWh — SE3'\n",
      "0xDEAD_BEEF + 0b1010 + 1_000_000\n",
      "unterminated = 'oops\n",
      '"""never closed\n',
    ];
    for (const source of sources) {
      expect(joined(tokenizeCode(source, "python"))).toBe(source);
      expect(joined(tokenizeCode(source, "r"))).toBe(source);
    }
  });

  it("colours Python comments, strings, numbers and keywords", () => {
    const source = 'if x == 3:  # note\n    y = "text"\n';
    expect(textOf(source, "keyword")).toEqual(["if"]);
    expect(textOf(source, "number")).toEqual(["3"]);
    expect(textOf(source, "comment")).toEqual(["# note"]);
    expect(textOf(source, "string")).toEqual(['"text"']);
  });

  it("takes a docstring whole, rather than closing an empty string on its second quote", () => {
    // The failure this ordering exists to prevent: `""` matching first would
    // leave the docstring's body as code and highlight the rest inside out.
    const source = '"""Doc with a # hash and a "quote" in it."""\nx = 1\n';
    expect(textOf(source, "string")).toEqual(['"""Doc with a # hash and a "quote" in it."""']);
    expect(textOf(source, "comment")).toEqual([]);
    expect(textOf(source, "number")).toEqual(["1"]);
  });

  it("does not treat a # inside a string as a comment", () => {
    expect(textOf('url = "https://x/#anchor"', "comment")).toEqual([]);
    expect(textOf('url = "https://x/#anchor"', "string")).toEqual(['"https://x/#anchor"']);
  });

  it("names the builtins an analyst's cell actually contains", () => {
    expect(textOf("print(len(rows))", "builtin").sort()).toEqual(["len", "print"]);
  });

  it("leaves ordinary identifiers plain, so a variable is not a keyword", () => {
    // `iffy` starts with `if`; a substring match would colour it.
    expect(textOf("iffy = formatted", "keyword")).toEqual([]);
    expect(kinds("iffy = formatted")).toEqual(["plain"]);
  });

  it("merges adjacent runs of one kind into one span", () => {
    // One span per line of plain code, not one per character class — this is
    // what keeps a 200-line cell from becoming thousands of DOM nodes.
    expect(kinds("a + b - c")).toEqual(["plain"]);
  });

  it("colours strings and numbers for a language it does not know, and nothing else", () => {
    // Honest by design: strings and numbers lex the same way nearly everywhere,
    // keywords and `#` comments do not. A half-right R highlighter is worse
    // than a plain one.
    const source = "x <- 42  # an R comment\ny <- 'text'\n";
    expect(textOf(source, "number", "r")).toEqual(["42"]);
    expect(textOf(source, "string", "r")).toEqual(["'text'"]);
    expect(textOf(source, "comment", "r")).toEqual([]);
    expect(textOf(source, "keyword", "r")).toEqual([]);
  });

  it.each(["python", "python3", "ipython", "PYTHON"])("treats %s as Python", (language) => {
    expect(textOf("import os  # sys", "keyword", language)).toEqual(["import"]);
    expect(textOf("import os  # sys", "comment", language)).toEqual(["# sys"]);
  });
});

describe("executionLabel", () => {
  it("distinguishes never-run from ran-and-printed-nothing", () => {
    expect(executionLabel(null)).toBe("[ ]");
    expect(executionLabel(0)).toBe("[0]");
    expect(executionLabel(12)).toBe("[12]");
  });
});

describe("countPhrase", () => {
  it("agrees on plurals and thousands so every cap notice reads the same", () => {
    expect(countPhrase(1, "cell")).toBe("1 cell");
    expect(countPhrase(0, "cell")).toBe("0 cells");
    expect(countPhrase(12_345, "character")).toBe("12,345 characters");
  });
});

describe("byteSize", () => {
  it.each([
    [0, "0 bytes"],
    [512, "512 bytes"],
    [1024, "1.0 KB"],
    [1536, "1.5 KB"],
    [1_048_576, "1.0 MB"],
    // The size an omitted-image notice actually reports: a screenshot pasted
    // into a report notebook, which the server refuses to put on the wire.
    [36_909_875, "35.2 MB"],
    [3 * 1024 ** 3, "3.0 GB"],
  ])("%s bytes reads as %s", (bytes, expected) => {
    expect(byteSize(bytes)).toBe(expected);
  });

  it("never reports a negative size, whatever it is handed", () => {
    // The number comes off the wire. A notice saying "-1.0 KB" would be a
    // viewer volunteering that it does not understand its own server.
    expect(byteSize(-1)).toBe("0 bytes");
  });
});
