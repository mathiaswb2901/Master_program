import { describe, expect, it } from "vitest";

import { MAX_PARAM_CHARS, paramsSchema, validateParams } from "./commandParams";

const declaration = {
  fields: [
    { name: "prompt", detail: "what to ask" },
    { name: "cwd", required: false, maxLength: 260, detail: "a folder" },
  ],
};

describe("the published schema", () => {
  it("mirrors the declaration into the wire shape the relay validates against", () => {
    expect(paramsSchema(declaration)).toEqual({
      params: [
        { name: "prompt", type: "string", required: true, max_length: null, detail: "what to ask" },
        { name: "cwd", type: "string", required: false, max_length: 260, detail: "a folder" },
      ],
    });
  });

  it("defaults a field to required, so an optional one has to say so", () => {
    expect(paramsSchema({ fields: [{ name: "name" }] }).params[0]?.required).toBe(true);
  });
});

describe("validating an incoming params object", () => {
  it("accepts what was declared and hands back only those keys", () => {
    const outcome = validateParams(declaration, { prompt: "hello", cwd: "src" });
    expect(outcome).toEqual({ ok: true, values: { prompt: "hello", cwd: "src" } });
  });

  it("omits an absent optional field rather than inventing an empty string", () => {
    const outcome = validateParams(declaration, { prompt: "hello" });
    expect(outcome.ok && outcome.values).toEqual({ prompt: "hello" });
    expect(outcome.ok && "cwd" in outcome.values).toBe(false);
  });

  // Every refusal names the field *and* what would be accepted: a caller told
  // only "invalid params" spends a round trip finding out which one, and a round
  // trip is the cost this seam exists to avoid.
  it("names an unknown field and lists the ones it takes", () => {
    const outcome = validateParams(declaration, { promt: "typo" });
    expect(outcome.ok).toBe(false);
    expect(!outcome.ok && outcome.detail).toContain("no parameter “promt”");
    expect(!outcome.ok && outcome.detail).toContain("prompt, cwd");
  });

  it("names a missing required field with its hint", () => {
    const outcome = validateParams(declaration, { cwd: "src" });
    expect(!outcome.ok && outcome.detail).toContain("needs “prompt” (what to ask)");
  });

  it("refuses a value that is not a string", () => {
    const outcome = validateParams(declaration, { prompt: 42 });
    expect(!outcome.ok && outcome.detail).toContain("must be a string, got number");
  });

  it("refuses a value past the field's own cap, naming the cap", () => {
    const outcome = validateParams(declaration, { prompt: "x", cwd: "y".repeat(261) });
    expect(!outcome.ok && outcome.detail).toContain("the limit is 260");
  });

  it("falls back to the shared ceiling when a field names none", () => {
    const long = "x".repeat(MAX_PARAM_CHARS + 1);
    const outcome = validateParams(declaration, { prompt: long });
    expect(!outcome.ok && outcome.detail).toContain(String(MAX_PARAM_CHARS));
    // …and one character under it is fine, so the bound is the bound.
    expect(validateParams(declaration, { prompt: "x".repeat(MAX_PARAM_CHARS) }).ok).toBe(true);
  });
});
