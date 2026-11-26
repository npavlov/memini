import { describe, expect, it } from "vitest";

import { WORKSPACE_HINT, WORKSPACE_MEANING, currentWorkspace } from "./workspaces";

describe("the workspace every screen shows", () => {
  it("is the one chosen while the server still has it", () => {
    expect(currentWorkspace(["harbour", "quay"], "quay")).toBe("quay");
  });

  it.each([
    ["nothing was chosen", undefined],
    ["the one chosen is gone", "lighthouse"],
  ])("is the first one when %s", (_when, chosen) => {
    expect(currentWorkspace(["harbour", "quay"], chosen)).toBe("harbour");
  });

  it("is none on a server without a workspace", () => {
    expect(currentWorkspace([], "harbour")).toBeNull();
  });

  it("is explained as a space kept apart that a token can be limited to", () => {
    expect(WORKSPACE_HINT).toContain("company or client");
    expect(WORKSPACE_MEANING).toContain("kept apart");
    expect(WORKSPACE_MEANING).toContain("token can be limited");
  });
});
