/**
 * The webview's half of the native Office host: units and the DPI boundary.
 *
 * The page is a courier here — it turns a `HostCommand` into a Tauri call and
 * acks it — so what is worth testing is exactly that translation, and the one
 * place two coordinate systems meet. Everything else about hosting is a real
 * window in another process, and is verified by running it.
 */

import { describe, expect, it } from "vitest";

import { cssRect, hostAppKind, physicalRect, runCommand, shellCallFor } from "./officeHost";
import type { HostCommand, PanelRect } from "./types";

const command = (over: Partial<HostCommand>): HostCommand => ({
  type: "host_command",
  command_id: "c1",
  host_id: "host-1",
  action: "set_bounds",
  window_id: null,
  rect: null,
  visible: null,
  ...over,
});

const RECT: PanelRect = { x: 250, y: 100, width: 800, height: 600 };

const domRect = (x: number, y: number, width: number, height: number): DOMRectReadOnly =>
  ({
    x,
    y,
    width,
    height,
    left: x,
    top: y,
    right: x + width,
    bottom: y + height,
  }) as DOMRectReadOnly;

describe("the unit boundary", () => {
  it("sends physical pixels, because that is what the wire is documented in", () => {
    expect(physicalRect(domRect(100, 40, 320, 240), 2.5)).toEqual({
      x: 250,
      y: 100,
      width: 800,
      height: 600,
    });
  });

  it("derives size from rounded edges, so two panels cannot leave a seam", () => {
    // Adjacent panels: one ends where the next begins. Rounding width
    // independently gives 250|250 from 249.6+250.4 — a pixel of nothing.
    const dpr = 1.5;
    const left = physicalRect(domRect(0, 0, 166.4, 100), dpr);
    const right = physicalRect(domRect(166.4, 0, 166.4, 100), dpr);
    expect(left.x + left.width).toBe(right.x);
  });

  it("round-trips exactly at the scale the shell reports", () => {
    // The Rust side multiplies by the same factor, so this divide and that
    // multiply have to cancel — a rectangle scaled twice is the classic bug
    // this seam exists to avoid.
    for (const dpr of [1, 1.25, 1.5, 2, 2.5]) {
      const css = cssRect(RECT, dpr);
      expect({
        x: Math.round(css.x * dpr),
        y: Math.round(css.y * dpr),
        width: Math.round(css.width * dpr),
        height: Math.round(css.height * dpr),
      }).toEqual(RECT);
    }
  });

  it("never asks for a zero-sized window", () => {
    const rect = physicalRect(domRect(10, 10, 0.2, 0.2), 1);
    expect(rect.width).toBeGreaterThan(0);
    expect(rect.height).toBeGreaterThan(0);
  });
});

describe("commands become shell calls", () => {
  it("maps every action to its Rust command", () => {
    const calls = (
      [
        command({ action: "embed", window_id: 4242, rect: RECT }),
        command({ action: "set_bounds", rect: RECT }),
        command({ action: "set_visible", visible: false }),
        command({ action: "detach" }),
        command({ action: "close" }),
      ] as HostCommand[]
    ).map((c) => shellCallFor(c, 2.5));
    expect(calls.map((call) => call?.command)).toEqual([
      "host_embed",
      "host_set_bounds",
      "host_set_visible",
      "host_detach",
      "host_close",
    ]);
    expect(calls[0]?.args).toMatchObject({ hostId: "host-1", windowId: 4242 });
    expect(calls[0]?.args.rect).toEqual({ x: 100, y: 40, width: 320, height: 240 });
    expect(calls[2]?.args).toEqual({ hostId: "host-1", visible: false });
  });

  it("refuses an embed with no window rather than calling with a wrong one", () => {
    expect(shellCallFor(command({ action: "embed", rect: RECT }), 1)).toBeNull();
    expect(shellCallFor(command({ action: "embed", window_id: 1 }), 1)).toBeNull();
  });

  it("refuses an action this build does not know", () => {
    // A server one version ahead. An honest refusal beats a call with the
    // wrong arguments, which the shell would answer by doing something else.
    expect(shellCallFor(command({ action: "levitate" as never }), 1)).toBeNull();
  });
});

describe("acks", () => {
  it("says ok when the shell did it", async () => {
    const seen: string[] = [];
    const ack = await runCommand(command({ action: "detach" }), 1, (call) => {
      seen.push(call.command);
      return Promise.resolve(null);
    });
    expect(seen).toEqual(["host_detach"]);
    expect(ack).toEqual({
      type: "host_command_ack",
      command_id: "c1",
      ok: true,
      code: null,
      message: null,
    });
  });

  it("carries the Rust refusal code back to the server", async () => {
    const ack = await runCommand(command({ action: "detach" }), 1, () =>
      Promise.reject({ code: "unknown_host", message: "no hosted panel with id host-1" }),
    );
    expect(ack.ok).toBe(false);
    expect(ack.code).toBe("unknown_host");
    expect(ack.message).toContain("host-1");
  });

  it("still answers when the failure is not a HostError at all", async () => {
    // An IPC that threw a plain Error — a shell that is not there, a command
    // that is not registered. Silence would leave the server waiting.
    const ack = await runCommand(command({ action: "close" }), 1, () =>
      Promise.reject(new Error("no shell")),
    );
    expect(ack).toMatchObject({ ok: false, code: null, message: "no shell" });
  });

  it("answers an unknown action instead of dropping it", async () => {
    const ack = await runCommand(command({ action: "levitate" as never }), 1, () =>
      Promise.reject(new Error("never called")),
    );
    expect(ack).toMatchObject({ ok: false, code: "unsupported" });
  });
});

describe("which application opens a document", () => {
  it("mirrors the server's table", () => {
    expect(hostAppKind("notes/report.docx")).toBe("word");
    expect(hostAppKind("BUDGET.XLSX")).toBe("excel");
    expect(hostAppKind("deck.pptx")).toBe("powerpoint");
    expect(hostAppKind("readme.md")).toBeNull();
    expect(hostAppKind("no-extension")).toBeNull();
  });
});
