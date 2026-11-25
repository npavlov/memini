import { describe, expect, it } from "vitest";

import { describeReadiness, describeTokenCheck, parseConnectionCheck } from "./connection-check";
import type { ConnectionCheck } from "./connection-check";

function check(changes: Partial<ConnectionCheck>): ConnectionCheck {
  return { version: "0.2.0", ready: true, token: "accepted", workspaces: 2, detail: null, ...changes };
}

describe("parseConnectionCheck", () => {
  it("reads the result of a check from the answer of the Rust side", () => {
    const parsed = parseConnectionCheck({
      version: "0.2.0",
      ready: false,
      token: "refused",
      workspaces: null,
      detail: "The token is not valid.",
    });

    expect(parsed).toEqual(
      check({ ready: false, token: "refused", workspaces: null, detail: "The token is not valid." }),
    );
  });

  it("reads a token state it does not know as unknown", () => {
    expect(parseConnectionCheck({ version: "0.2.0", token: "made_up" }).token).toBe("unknown");
  });
});

describe("describeReadiness", () => {
  it.each([
    [true, "good", "is ready"],
    [false, "warn", "not ready yet"],
    [null, "info", "answers"],
  ] as const)("describes a server whose readiness is %j", (ready, tone, words) => {
    const finding = describeReadiness(check({ ready }));

    expect(finding.tone).toBe(tone);
    expect(finding.text).toContain(words);
  });

  it("names the version the server reports", () => {
    expect(describeReadiness(check({})).text).toContain("version 0.2.0");
  });
});

describe("describeTokenCheck", () => {
  it.each([
    ["missing", "warn", "No token is stored"],
    ["accepted", "good", "may read 2 workspaces"],
    ["refused", "bad", "refused the token"],
    ["no_read_scope", "warn", "no read scope"],
    ["unknown", "warn", "could not be checked"],
  ] as const)("describes a token that is %s", (token, tone, words) => {
    const finding = describeTokenCheck(check({ token }));

    expect(finding.tone).toBe(tone);
    expect(finding.text).toContain(words);
  });

  it("uses the singular for one workspace", () => {
    expect(describeTokenCheck(check({ workspaces: 1 })).text).toContain("1 workspace.");
  });
});
