import { describe, expect, it } from "vitest";

import type { Connection } from "../connections/connections";
import { AppError } from "../ipc/app-error";
import { createFakeBackend } from "../ipc/fake-backend";
import type { FakeAnswer } from "../ipc/fake-backend";
import { barItems, readConnectionHealth } from "./connection-health";
import type { ConnectionHealth } from "./connection-health";
import type { ServiceStatus } from "./status";

const connection: Connection = {
  id: "c1",
  name: "Harbour team",
  kind: "remote",
  url: "https://memini.example.org",
  allowHttp: false,
  hasToken: true,
};

function status(current: boolean): ServiceStatus {
  return {
    version: "0.2.0",
    asOf: null,
    current,
    databaseReachable: true,
    schemaVersion: 31,
    expectedSchemaVersion: 31,
    indexState: "ok",
    indexModel: null,
    indexNote: "",
    workspaces: [],
  };
}

function health(changes: Partial<ConnectionHealth>): ConnectionHealth {
  return {
    answers: true,
    problem: null,
    version: "0.2.0",
    ready: true,
    token: "accepted",
    mcp: "on",
    status: status(true),
    statusProblem: null,
    ...changes,
  };
}

/** The answers of a server that is up, with a token that may read. */
const answers: Readonly<Record<string, FakeAnswer>> = {
  connection_check: () => ({ version: "0.2.0", ready: true, token: "accepted", workspaces: 1 }),
  mcp_probe: () => "on",
  api_status: () => ({ version: "0.2.0", current: true, database: {}, index: {}, workspaces: [] }),
};

function labels(given: ConnectionHealth): readonly string[] {
  return barItems(given).map((item) => item.label);
}

describe("readConnectionHealth", () => {
  it("reports a server that answers, its version, the base and MCP", async () => {
    const read = await readConnectionHealth(createFakeBackend({ answers }), connection);

    expect(read).toMatchObject({ answers: true, version: "0.2.0", ready: true, token: "accepted", mcp: "on" });
    expect(read.status?.current).toBe(true);
  });

  it("reports a server that does not answer with the reason and nothing else", async () => {
    const down = new AppError({ kind: "unreachable", message: "Cannot reach the server." });
    const backend = createFakeBackend({
      answers: {
        ...answers,
        connection_check: () => {
          throw down;
        },
      },
    });

    const read = await readConnectionHealth(backend, connection);

    expect(read).toMatchObject({ answers: false, problem: down, version: null, mcp: null, status: null });
    expect(backend.calls.map((call) => call.command)).toEqual(["connection_check"]);
  });

  it("does not ask for the status with a token that may not read", async () => {
    const backend = createFakeBackend({
      answers: { ...answers, connection_check: () => ({ version: "0.2.0", ready: true, token: "missing" }) },
    });

    const read = await readConnectionHealth(backend, connection);

    expect(read).toMatchObject({ answers: true, token: "missing", status: null, statusProblem: null });
    expect(backend.calls.map((call) => call.command)).not.toContain("api_status");
  });

  it("keeps the rest when the status alone fails", async () => {
    const away = new AppError({ kind: "unreachable", message: "The database is not available." });
    const backend = createFakeBackend({
      answers: {
        ...answers,
        api_status: () => {
          throw away;
        },
      },
    });

    const read = await readConnectionHealth(backend, connection);

    expect(read).toMatchObject({ answers: true, mcp: "on", status: null, statusProblem: away });
  });
});

describe("barItems", () => {
  it("says that the server answers, its version, that the base is current and that MCP is on", () => {
    expect(labels(health({}))).toEqual(["server answers", "version 0.2.0", "base current", "MCP on"]);
  });

  it("says only that the server does not answer when it does not", () => {
    const silent = health({ answers: false, version: null, token: null, mcp: null, status: null });

    expect(barItems(silent)).toEqual([{ key: "server", tone: "bad", label: "server does not answer" }]);
  });

  it.each([
    ["is behind", health({ status: status(false) }), "bad", "base behind"],
    ["cannot be read without a token", health({ status: null, token: "missing" }), "warn", "base unknown: no token"],
    ["cannot be read with a refused token", health({ status: null, token: "refused" }), "bad", "base unknown: token refused"],
    ["cannot be read without the read scope", health({ status: null, token: "no_read_scope" }), "idle", "base unknown: no read scope"],
    ["could not be read", health({ status: null }), "warn", "base unknown"],
  ])("says that the base %s", (_what, given, tone, label) => {
    expect(barItems(given)[2]).toEqual({ key: "base", tone, label });
  });

  it.each([
    ["on", "good", "MCP on"],
    ["off", "idle", "MCP off"],
    ["unknown", "warn", "MCP unknown"],
    [null, "warn", "MCP unknown"],
  ] as const)("says whether MCP is on when the endpoint is %j", (mcp, tone, label) => {
    expect(barItems(health({ mcp }))[3]).toEqual({ key: "mcp", tone, label });
  });

  it("warns when the server answers but is not ready", () => {
    expect(barItems(health({ ready: false }))[0]).toMatchObject({ tone: "warn", label: "server answers, not ready" });
  });
});
