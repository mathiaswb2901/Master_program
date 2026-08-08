/**
 * The Setup descriptor's eager half: the status chip's visibility (the quiet-bar
 * rule) and the dismissal it persists.
 *
 * `./dock` reaches the whole registry (and Monaco) through `./tools`; it is
 * mocked so this file tests the chip and the store in isolation. `./api` is
 * mocked so `refreshStatus` (fired by nothing here) never touches the network.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import type { SetupStatus } from "./types";

const closePanel = vi.fn();
const openPanel = vi.fn();
vi.mock("./dock", () => ({
  closePanel: (id: string) => closePanel(id),
  openPanel: (id: string) => openPanel(id),
}));
const putFileContent = vi.fn((_body: unknown) => Promise.resolve({}));
vi.mock("./api", () => ({
  ApiError: class ApiError extends Error {},
  getFileContent: () => Promise.reject(new Error("not called")),
  getLayouts: () => Promise.resolve({ state: { current: null } }),
  getSetupStatus: () => Promise.resolve(null),
  putFileContent: (body: unknown) => putFileContent(body),
}));

import { dismissSetup, setupChipLabel, useSetup } from "./setup";

function status(over: Partial<SetupStatus> = {}): SetupStatus {
  return {
    checks: [
      {
        id: "claude_login",
        title: "Claude",
        state: "action_needed",
        detail: "not signed in",
        action: { kind: "instruction", label: "x", command_id: null, instruction: "claude /login" },
      },
    ],
    first_run: true,
    all_ok: false,
    ...over,
  };
}

afterEach(() => {
  useSetup.setState({ dismissed: null, status: null });
  closePanel.mockClear();
  openPanel.mockClear();
  putFileContent.mockClear();
});

describe("the Setup status chip reading (quiet-bar rule)", () => {
  it("shows nothing before the first read", () => {
    expect(setupChipLabel(false, null)).toBeNull();
  });

  it("shows nothing once everything is connected (all_ok)", () => {
    expect(setupChipLabel(false, status({ all_ok: true }))).toBeNull();
  });

  it("retires once the walkthrough is dismissed, even with something unconnected", () => {
    expect(setupChipLabel(true, status())).toBeNull();
  });

  it("shows nothing until the workspace has answered (dismissed null)", () => {
    expect(setupChipLabel(null, status())).toBeNull();
  });

  it("offers the way in while it is unanswered and something needs action", () => {
    // The count of action-needed checks rides along.
    expect(setupChipLabel(false, status())).toBe("Setup (1)");
  });

  it("drops the count when nothing is action_needed but it is still worth showing", () => {
    // Defensive: all_ok is the real gate; a status with no action_needed is
    // all_ok, so this asserts the label shape rather than a reachable state.
    const twoActions = status({
      checks: [
        { id: "claude_login", title: "Claude", state: "action_needed", detail: "", action: null },
        { id: "office", title: "Office", state: "action_needed", detail: "", action: null },
      ],
    });
    expect(setupChipLabel(false, twoActions)).toBe("Setup (2)");
  });
});

describe("dismissal", () => {
  it("marks dismissed, persists it, and retires the tab", () => {
    dismissSetup();
    expect(useSetup.getState().dismissed).toBe(true);
    expect(putFileContent).toHaveBeenCalledWith({
      path: ".workbench/setup.json",
      content: `${JSON.stringify({ dismissed: true }, null, 2)}\n`,
    });
    expect(closePanel).toHaveBeenCalledWith("setup");
  });
});
