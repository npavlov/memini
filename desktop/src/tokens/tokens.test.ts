import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { ShapeError } from "../json/read-json";
import {
  EMPTY_TOKEN_FORM,
  checkNewToken,
  parseCreatedToken,
  parseTokenList,
  parseWorkspaceNames,
  toNewTokenRequest,
  toggleScope,
  tokenTone,
} from "./tokens";
import type { NewTokenForm } from "./tokens";
import { createToken, revokeToken } from "./tokens-api";

/** A value with the format of a token, assembled here so that no literal of one is committed. */
const SECRET = `memini_${"a".repeat(43)}`;

const listed = {
  id: "0b9f6a52-7c1e-4d7a-9a57-3f6f0c2d8e11",
  name: "laptop agent",
  prefix: "memini_aaaaaaaa",
  scopes: ["read"],
  workspace: "harbour",
  status: "active",
  created_at: "2026-11-20T09:00:00Z",
  expires_at: null,
  revoked_at: null,
  last_used_at: "2026-11-23T08:00:00Z",
};

function form(changes: Partial<NewTokenForm>): NewTokenForm {
  return { ...EMPTY_TOKEN_FORM, name: "laptop agent", ...changes };
}

describe("parseTokenList", () => {
  it("reads the tokens of the API's list, which holds no secret", () => {
    const tokens = parseTokenList({ items: [listed] });

    expect(tokens).toEqual([
      {
        id: listed.id,
        name: "laptop agent",
        prefix: "memini_aaaaaaaa",
        scopes: ["read"],
        workspace: "harbour",
        status: "active",
        createdAt: "2026-11-20T09:00:00Z",
        expiresAt: null,
        revokedAt: null,
        lastUsedAt: "2026-11-23T08:00:00Z",
      },
    ]);
  });

  it("puts a name with line breaks on one line", () => {
    const tokens = parseTokenList({ items: [{ ...listed, name: "laptop\nagent" }] });

    expect(tokens[0]?.name).toBe("laptop agent");
  });

  it("throws a ShapeError when a token has no id", () => {
    expect(() => parseTokenList({ items: [{ name: "x" }] })).toThrow(ShapeError);
  });

  it("throws a ShapeError when the answer is not an object", () => {
    expect(() => parseTokenList("<html>")).toThrow(ShapeError);
  });
});

describe("parseCreatedToken", () => {
  it("reads the new token with its secret", () => {
    const created = parseCreatedToken({ ...listed, token: SECRET });

    expect(created.secret).toBe(SECRET);
    expect(created.info.name).toBe("laptop agent");
  });

  it.each([
    ["is missing", undefined],
    ["is empty", ""],
    ["holds a line break", `${SECRET}\nmore`],
    ["holds markup with spaces", "<b>not a token</b>"],
    ["is very long", "a".repeat(201)],
  ])("throws a ShapeError when the secret %s", (_what, token) => {
    expect(() => parseCreatedToken({ ...listed, token })).toThrow(ShapeError);
  });
});

describe("parseWorkspaceNames", () => {
  it("returns the names of the workspaces", () => {
    const names = parseWorkspaceNames({ items: [{ name: "harbour" }, { name: "quay-2" }] });

    expect(names).toEqual(["harbour", "quay-2"]);
  });

  it("drops a name that is not a workspace handle", () => {
    const names = parseWorkspaceNames({ items: [{ name: "Harbour <b>" }, { name: "quay" }] });

    expect(names).toEqual(["quay"]);
  });
});

describe("checkNewToken", () => {
  it("accepts a named token with a scope", () => {
    expect(checkNewToken(form({}))).toBeNull();
  });

  it.each([
    ["has no name", form({ name: " " }), "what the token is for"],
    ["has a name of 201 characters", form({ name: "n".repeat(201) }), "at most 200"],
    ["has no scope", form({ scopes: [] }), "at least one scope"],
    ["names a workspace that is not a handle", form({ workspace: "Harbour Team" }), "lowercase"],
  ])("reports the problem when the form %s", (_what, invalid, words) => {
    expect(checkNewToken(invalid)).toContain(words);
  });
});

describe("toggleScope", () => {
  it("adds a scope in the canonical order", () => {
    expect(toggleScope(["admin"], "read")).toEqual(["read", "admin"]);
  });

  it("removes a scope that is chosen", () => {
    expect(toggleScope(["read", "admin"], "read")).toEqual(["admin"]);
  });
});

describe("toNewTokenRequest", () => {
  it("sends null for every workspace and for no expiry", () => {
    expect(toNewTokenRequest(form({}))).toEqual({
      name: "laptop agent",
      scopes: ["read"],
      workspace: null,
      expires_in_seconds: null,
    });
  });

  it("sends the workspace and the lifetime in seconds", () => {
    const request = toNewTokenRequest(form({ workspace: "harbour", expiry: "30d" }));

    expect(request).toMatchObject({ workspace: "harbour", expires_in_seconds: 2_592_000 });
  });
});

describe("tokenTone", () => {
  it.each([
    ["active", "good"],
    ["expired", "warn"],
    ["revoked", "idle"],
  ])("colours a token that is %s", (status, tone) => {
    expect(tokenTone(status)).toBe(tone);
  });
});

describe("the token commands", () => {
  it("create a token for a connection by its id and return the secret once", async () => {
    const backend = createFakeBackend({
      answers: { api_token_create: () => ({ ...listed, token: SECRET }) },
    });

    const created = await createToken(backend, "c1", form({}));

    expect(created.secret).toBe(SECRET);
    expect(backend.calls).toEqual([
      {
        command: "api_token_create",
        args: { id: "c1", request: toNewTokenRequest(form({})) },
      },
    ]);
  });

  it("revoke a token by its id", async () => {
    const backend = createFakeBackend({ answers: { api_token_revoke: () => listed } });

    await revokeToken(backend, "c1", listed.id);

    expect(backend.calls).toEqual([
      { command: "api_token_revoke", args: { id: "c1", tokenId: listed.id } },
    ]);
  });
});
