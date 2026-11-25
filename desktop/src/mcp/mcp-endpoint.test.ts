import { describe, expect, it } from "vitest";

import type { Connection } from "../connections/connections";
import type { Backend } from "../ipc/backend";
import { createFakeBackend } from "../ipc/fake-backend";
import type { LauncherStatus } from "../server/launcher";
import {
  describeEndpoint,
  endpointAddress,
  launcherEndpointState,
  probeEndpoint,
} from "./mcp-endpoint";

const local: Connection = {
  id: "c1",
  name: "This computer",
  kind: "local",
  url: "http://127.0.0.1:6364",
  allowHttp: false,
  hasToken: true,
};

const remote: Connection = { ...local, id: "c2", kind: "remote", url: "https://memini.example.org" };

function launcher(changes: Partial<LauncherStatus>): LauncherStatus {
  return {
    installed: true,
    running: true,
    apiUrl: "http://127.0.0.1:7001",
    mcpUrl: "http://127.0.0.1:7001/mcp",
    mcpEnabled: true,
    image: null,
    directory: null,
    dockerAvailable: true,
    dockerError: null,
    services: [],
    ...changes,
  };
}

function backendAnswering(answer: unknown): Backend {
  return createFakeBackend({ answers: { mcp_probe: () => answer } });
}

describe("endpointAddress", () => {
  it("uses the launcher's MCP address for the server on this computer", () => {
    expect(endpointAddress(local, launcher({}))).toBe("http://127.0.0.1:7001/mcp");
  });

  it("derives the address from the connection when the launcher is not there", () => {
    expect(endpointAddress(local, null)).toBe("http://127.0.0.1:6364/mcp");
  });

  it("ignores a launcher address that is not on this computer", () => {
    const elsewhere = launcher({ mcpUrl: "https://elsewhere.example.org/mcp" });

    expect(endpointAddress(local, elsewhere)).toBe("http://127.0.0.1:6364/mcp");
  });

  it("never uses the launcher's address for a remote connection", () => {
    expect(endpointAddress(remote, launcher({}))).toBe("https://memini.example.org/mcp");
  });
});

describe("launcherEndpointState", () => {
  it.each([
    [true, "on"],
    [false, "off"],
    [null, null],
  ])("reads the launcher's mcp_enabled %j as %j", (mcpEnabled, state) => {
    expect(launcherEndpointState(launcher({ mcpEnabled }))).toBe(state);
  });

  it("returns null without a launcher", () => {
    expect(launcherEndpointState(null)).toBeNull();
  });
});

describe("probeEndpoint", () => {
  it.each([
    ["on", "on"],
    ["off", "off"],
    ["unknown", "unknown"],
    ["something else", "unknown"],
    [null, "unknown"],
  ])("reads the answer %j of the Rust side as %s", async (answer, state) => {
    expect(await probeEndpoint(backendAnswering(answer), "c2")).toBe(state);
  });
});

describe("describeEndpoint", () => {
  it.each([
    ["on", "good", "On"],
    ["off", "idle", "Off"],
    ["unknown", "warn", "Unknown"],
  ] as const)("labels an endpoint that is %s", (state, tone, label) => {
    expect(describeEndpoint(state)).toMatchObject({ tone, label });
  });
});
