import { describe, expect, it } from "vitest";

import { ShapeError } from "../json/read-json";
import {
  describePhase,
  installSteps,
  parseLauncherInfo,
  parseLauncherResult,
  parseLauncherStatus,
  serverPhase,
  serviceTone,
} from "./launcher";
import type { LauncherStatus, ServiceState } from "./launcher";

function service(changes: Partial<ServiceState>): ServiceState {
  return { name: "memini", state: "running", health: "healthy", ...changes };
}

function status(changes: Partial<LauncherStatus>): LauncherStatus {
  return {
    installed: true,
    running: true,
    apiUrl: "http://127.0.0.1:6364",
    mcpUrl: "http://127.0.0.1:6364/mcp",
    mcpEnabled: true,
    image: "registry.example.org/memini:0.2.0",
    directory: "/home/sam/.local/share/memini/server",
    dockerAvailable: true,
    dockerError: null,
    services: [service({ name: "db" }), service({}), service({ name: "ollama", health: null })],
    ...changes,
  };
}

describe("parseLauncherStatus", () => {
  it("reads the status the Rust side parsed from the launcher", () => {
    const parsed = parseLauncherStatus({
      installed: true,
      running: true,
      api_url: "http://127.0.0.1:6364",
      mcp_url: "http://127.0.0.1:6364/mcp",
      mcp_enabled: true,
      image: "registry.example.org/memini:0.2.0",
      directory: "/home/sam/.local/share/memini/server",
      docker: { available: true, error: null },
      services: [
        { name: "db", state: "running", health: "healthy" },
        { name: "memini", state: "running", health: "healthy" },
        { name: "ollama", state: "running", health: null },
      ],
    });

    expect(parsed).toEqual(status({}));
  });

  it("reads a computer without Docker or a server", () => {
    const parsed = parseLauncherStatus({
      installed: false,
      running: false,
      api_url: null,
      mcp_url: null,
      mcp_enabled: null,
      image: null,
      directory: null,
      docker: { available: false, error: "docker was not found" },
      services: [],
    });

    expect(parsed).toMatchObject({
      installed: false,
      mcpEnabled: null,
      dockerAvailable: false,
      dockerError: "docker was not found",
      services: [],
    });
  });

  it("throws a ShapeError when the Docker report is missing", () => {
    expect(() => parseLauncherStatus({ installed: true })).toThrow(ShapeError);
  });
});

describe("parseLauncherResult", () => {
  it("reads a failure and puts its reason on one line", () => {
    const result = parseLauncherResult({ ok: false, error: "port 6364\nis taken" });

    expect(result).toEqual({ ok: false, error: "port 6364 is taken" });
  });

  it("reads an answer without ok as a failure", () => {
    expect(parseLauncherResult({}).ok).toBe(false);
  });
});

describe("parseLauncherInfo", () => {
  it("reads where the launcher is and whether the user set the path", () => {
    const info = parseLauncherInfo({
      configured_path: null,
      resolved_path: "/opt/homebrew/bin/memini-server",
    });

    expect(info).toEqual({ configuredPath: null, resolvedPath: "/opt/homebrew/bin/memini-server" });
  });
});

describe("serverPhase", () => {
  it.each([
    ["Docker is not available", status({ dockerAvailable: false }), "no_docker"],
    ["nothing is installed", status({ installed: false, running: false }), "not_installed"],
    ["the server is stopped", status({ running: false }), "stopped"],
    ["a service is still starting", status({ services: [service({ health: "starting" })] }), "starting"],
    ["a service is not up", status({ services: [service({ state: "exited" })] }), "starting"],
    ["a service is unhealthy", status({ services: [service({ health: "unhealthy" })] }), "unhealthy"],
    ["every service is up", status({}), "running"],
  ])("is decided by the first thing that is missing: %s", (_what, given, phase) => {
    expect(serverPhase(given)).toBe(phase);
  });

  it("is described with a title for every phase", () => {
    expect(describePhase("no_docker").title).toBe("Docker is not available");
  });
});

describe("serviceTone", () => {
  it.each([
    ["healthy", service({}), "good"],
    ["without a health check", service({ health: null }), "good"],
    ["starting", service({ health: "starting" }), "warn"],
    ["unhealthy", service({ health: "unhealthy" }), "bad"],
    ["exited", service({ state: "exited", health: null }), "idle"],
  ])("colours a service that is %s", (_what, given, tone) => {
    expect(serviceTone(given)).toBe(tone);
  });
});

describe("installSteps", () => {
  it.each([
    ["macos", "brew install npavlov/memini/memini-server"],
    ["linux", "brew install npavlov/memini/memini-server"],
    ["windows", "scoop install memini/memini-server"],
  ])("gives the README's install command for %s", (os, command) => {
    const text = installSteps(os)
      .map((step) => step.text)
      .join("\n");

    expect(text).toContain(command);
  });
});
