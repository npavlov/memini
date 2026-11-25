import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import { createFakeBackend } from "../ipc/fake-backend";
import { checkSteps, describeToken, parseConnectionCheck, tokenProblem } from "./connection-check";
import type { ConnectionCheck, TokenCheck } from "./connection-check";
import type { Connection } from "./connections";
import { checkConnection, testConnection } from "./connections-api";

const connection: Connection = {
  id: "c1",
  name: "Harbour team",
  kind: "remote",
  url: "http://127.0.0.1:6365",
  allowHttp: false,
  hasToken: true,
};

/** A test in which everything is well: the server answers and accepts a token with every scope. */
function check(changes: Partial<ConnectionCheck>): ConnectionCheck {
  return {
    unreachable: null,
    version: "0.2.0",
    ready: true,
    token: "accepted",
    lacking: null,
    scopes: ["read", "ingest", "admin"],
    workspaces: 2,
    detail: null,
    ...changes,
  };
}

const down = new AppError({ kind: "unreachable", message: "The server at http://127.0.0.1:6365 cannot be reached." });

/** Each step as its id, its state and its title. */
function outline(given: ConnectionCheck): readonly string[] {
  return checkSteps(given).map((step) => `${step.id}: ${step.state}: ${step.title}`);
}

function detailOf(given: ConnectionCheck, id: string): string | null {
  return checkSteps(given).find((step) => step.id === id)?.detail ?? null;
}

describe("parseConnectionCheck", () => {
  it("reads the result of a test from the answer of the Rust side", () => {
    const parsed = parseConnectionCheck({
      unreachable: null,
      version: "0.2.0",
      ready: false,
      token: "lacks_scope",
      lacking: "read",
      scopes: ["admin"],
      workspaces: null,
      detail: "The token lacks the read scope.",
    });

    expect(parsed).toEqual(
      check({ ready: false, token: "lacks_scope", lacking: "read", scopes: ["admin"], workspaces: null, detail: "The token lacks the read scope." }),
    );
  });

  it("reads a server that did not answer as part of the answer, with the reason", () => {
    const parsed = parseConnectionCheck({
      unreachable: { kind: "unreachable", message: "The server at http://127.0.0.1:6365 cannot be reached." },
      token: "missing",
    });

    expect([parsed.unreachable, parsed.version, parsed.token, parsed.scopes]).toEqual([down, null, "missing", null]);
  });

  it("reads a token state it does not know as unknown", () => {
    expect(parseConnectionCheck({ version: "0.2.0", token: "made_up" }).token).toBe("unknown");
  });
});

describe("checkSteps", () => {
  it("shows every step done for a server that accepts a token with every scope", () => {
    expect(outline(check({}))).toEqual([
      "address: done: The address answers",
      "version: done: Memini 0.2.0",
      "stored: done: A token is stored",
      "accepted: done: The server accepts the token",
      "scopes: done: Scopes: read, ingest, admin",
    ]);
  });

  it("names a connection without a token, and leaves the steps before it done", () => {
    const steps = outline(check({ token: "missing", scopes: null, workspaces: null }));

    expect(steps).toEqual([
      "address: done: The address answers",
      "version: done: Memini 0.2.0",
      "stored: failed: No token",
      "accepted: waiting: The server accepts the token",
    ]);
  });

  it("says what to do about a connection without a token", () => {
    const detail = detailOf(check({ token: "missing", scopes: null }), "stored");

    expect(detail).toBe("Add a token: paste it or choose a file.");
  });

  it("stops at an address that does not answer, and still says that no token is stored", () => {
    const steps = outline(check({ unreachable: down, version: null, ready: null, token: "missing", scopes: null }));

    expect(steps).toEqual([
      "address: failed: The address does not answer",
      "version: waiting: The version of the server",
      "stored: failed: No token",
      "accepted: waiting: The server accepts the token",
    ]);
  });

  it("says why an address does not answer and what to check", () => {
    const detail = detailOf(check({ unreachable: down, version: null, token: "not_checked", scopes: null }), "address");

    expect(detail).toBe(
      "The server at http://127.0.0.1:6365 cannot be reached. Check the address and that the server is running; the server of this computer is started on the Server screen.",
    );
  });

  it.each<[string, Partial<ConnectionCheck>, string, string]>([
    ["a refused token", { token: "refused", scopes: null }, "accepted: failed: Token refused", "The server does not know this token, or it is revoked or expired. Replace it: paste a new token or choose a file."],
    ["a token without the read scope", { token: "lacks_scope", lacking: "read", scopes: null }, "accepted: failed: The token lacks the read scope", "The token is valid, but the status of the base is read with the read scope. Use a token that has it: one with the admin scope creates it on the Tokens screen."],
    ["a token that could not be tried", { token: "unknown", scopes: null, detail: "The database is away." }, "accepted: failed: The token could not be tried", "Test again in a moment. The server said: The database is away."],
    ["a token not tried on a silent server", { unreachable: down, token: "not_checked", scopes: null }, "accepted: waiting: The server accepts the token", "Not tried: the address does not answer."],
  ])("reports %s with what to do next", (_case, changes, line, detail) => {
    const given = check(changes);

    expect([outline(given)[3], detailOf(given, "accepted")]).toEqual([line, detail]);
  });

  it("does not take an address that reports no version for a Memini server", () => {
    const steps = outline(check({ version: null, ready: null, token: "missing", scopes: null }));

    expect(steps[1]).toBe("version: failed: No Memini server at this address");
  });

  it("says that a server is not ready yet", () => {
    const detail = detailOf(check({ ready: false }), "version");

    expect(detail).toBe("It is not ready yet: it is starting, or its database does not answer.");
  });

  it("says what a token without a scope cannot open", () => {
    const detail = detailOf(check({ scopes: ["read"] }), "scopes");

    expect(detail).toBe(
      "Without ingest, the project map cannot be saved. Without admin, the Tokens screen and Connect for agent clients are closed.",
    );
  });

  it("says that the server did not tell the scopes of a token without the admin scope", () => {
    const given = check({ scopes: null });

    expect([outline(given)[4], detailOf(given, "scopes")]).toEqual([
      "scopes: waiting: The scopes of the token",
      "Not told: the server lists a token’s scopes only for a token with the admin scope.",
    ]);
  });
});

describe("tokenProblem", () => {
  it.each<[string, boolean, TokenCheck | null, string | null]>([
    ["a connection without a token, before the server was asked", false, null, "No token"],
    ["a connection without a token", false, "missing", "No token"],
    ["a token the keychain no longer has", true, "missing", "No token"],
    ["a refused token", true, "refused", "Token refused"],
    ["an accepted token", true, "accepted", null],
    ["a token that lacks a scope", true, "lacks_scope", null],
    ["a token not asked about yet", true, null, null],
  ])("for %s", (_case, hasToken, token, label) => {
    const problem = tokenProblem({ ...connection, hasToken }, token);

    expect(problem?.label ?? null).toBe(label);
  });

  it("says what to do and names the button for a missing token", () => {
    const problem = tokenProblem({ ...connection, hasToken: false }, null);

    expect(problem).toEqual({
      kind: "missing",
      tone: "warn",
      label: "No token",
      text: "This connection has no token, so the server gives the app nothing to show. Add a token: paste it or choose a file.",
      action: "Add a token",
    });
  });

  it("shows a refused token in the colour of a failure", () => {
    const problem = tokenProblem(connection, "refused");

    expect([problem?.tone, problem?.action]).toEqual(["bad", "Replace the token"]);
  });
});

describe("describeToken", () => {
  it.each<[TokenCheck, string, string]>([
    ["missing", "warn", "This connection has no token, so the server gives the app nothing to show. Add a token: paste it or choose a file."],
    ["refused", "bad", "The server refused the stored token: it does not know it, or it is revoked or expired. Replace it: paste a new token or choose a file."],
    ["lacks_scope", "warn", "The token is valid but lacks the read scope."],
    ["accepted", "good", "The server accepts the token."],
    ["unknown", "warn", "The token could not be tried."],
    ["not_checked", "warn", "The token could not be tried."],
  ])("describes a token that is %s", (token, tone, text) => {
    expect(describeToken(token, "read")).toEqual({ tone, text });
  });
});

describe("the commands of the Rust side", () => {
  it("asks for the scopes in the full test, and not in the check the status bar repeats", async () => {
    const backend = createFakeBackend({ answers: { connection_check: () => ({ version: "0.2.0", token: "missing" }) } });

    await testConnection(backend, "c1");
    await checkConnection(backend, "c1");

    expect(backend.calls).toEqual([
      { command: "connection_check", args: { id: "c1", scopes: true } },
      { command: "connection_check", args: { id: "c1", scopes: false } },
    ]);
  });
});
