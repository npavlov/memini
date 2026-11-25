import { describe, expect, it } from "vitest";

import type { Connection } from "../connections/connections";
import { createFakeBackend } from "../ipc/fake-backend";
import type { FakeAnswer } from "../ipc/fake-backend";
import { ShapeError } from "../json/read-json";
import type { LauncherStatus } from "./launcher";
import {
  DOCKER_INSTALL_PAGE,
  NOT_STARTED,
  busyReason,
  dockerAdvice,
  needsSetup,
  parseSetupOutcome,
  setupSteps,
  tokenExists,
} from "./setup";
import type { SetupProgress } from "./setup";
import { runSetup } from "./setup-run";
import type { Stopped } from "./setup-run";

function status(changes: Partial<LauncherStatus>): LauncherStatus {
  return {
    installed: true,
    running: true,
    apiUrl: "http://127.0.0.1:6364",
    mcpUrl: null,
    mcpEnabled: true,
    image: null,
    directory: null,
    dockerAvailable: true,
    dockerError: null,
    services: [],
    ...changes,
  };
}

const local: Connection = {
  id: "c1",
  name: "This computer",
  kind: "local",
  url: "http://127.0.0.1:6364",
  allowHttp: false,
  hasToken: true,
};

/** The view the Rust side answers with after the setup: the local connection, with a token. */
const view = {
  active_id: "c1",
  connections: [{ id: "c1", name: "This computer", kind: "local", url: local.url, has_token: true }],
};

/** The launcher's status as the Rust side passes it on. */
function wire(changes: Record<string, unknown>): unknown {
  return {
    installed: false,
    running: false,
    api_url: null,
    mcp_url: null,
    mcp_enabled: null,
    image: null,
    directory: null,
    docker: { available: true, error: null },
    services: [],
    ...changes,
  };
}

interface Run {
  readonly commands: readonly string[];
  readonly progress: readonly SetupProgress[];
  readonly stopped: readonly Stopped[];
  readonly returned: boolean;
}

async function run(answers: Readonly<Record<string, FakeAnswer>>, force = false): Promise<Run> {
  const backend = createFakeBackend({ answers });
  const progress: SetupProgress[] = [];
  const stopped: Stopped[] = [];
  const returned = await runSetup(backend, {
    force,
    onProgress: (step) => progress.push(step),
    onStopped: (found) => stopped.push(found),
  });
  return { commands: backend.calls.map((call) => call.command), progress, stopped, returned: returned !== null };
}

describe("dockerAdvice", () => {
  it("returns null when Docker is there", () => {
    expect(dockerAdvice(status({}))).toBeNull();
  });

  it("says to install Docker, with the page, when the command was not found", () => {
    const advice = dockerAdvice(
      status({ dockerAvailable: false, dockerError: "the docker command was not found; install Docker" }),
    );

    expect(advice).toMatchObject({ title: "Docker is not installed", page: DOCKER_INSTALL_PAGE });
  });

  it.each([
    ["docker failed with exit status 1; start the Docker engine, with Compose 2.24 or later"],
    ["docker did not answer in 20 seconds"],
    [null],
  ])("says to start Docker when the launcher reports %j", (dockerError) => {
    const advice = dockerAdvice(status({ dockerAvailable: false, dockerError }));

    expect(advice).toMatchObject({ title: "Docker is not running", page: null });
  });
});

describe("needsSetup", () => {
  it.each([
    ["no server is installed", status({ installed: false, running: false }), local, true],
    ["this computer is not a connection", status({}), null, true],
    ["the connection has no token", status({}), { ...local, hasToken: false }, true],
    ["the server is installed and the app holds its token", status({}), local, false],
    ["the server is only stopped", status({ running: false }), local, false],
  ])("tells whether there is something to set up when %s", (_what, given, connection, expected) => {
    expect(needsSetup(given, connection)).toBe(expected);
  });
});

describe("setupSteps", () => {
  it("shows every step as waiting before the start", () => {
    expect(setupSteps(NOT_STARTED).map((step) => step.state)).toEqual(["waiting", "waiting", "waiting"]);
  });

  it("shows the steps before the current one as done and says what the current one does", () => {
    const steps = setupSteps({ at: "server", failure: null });

    expect(steps.map((step) => step.state)).toEqual(["done", "running", "waiting"]);
    expect(steps[1]?.detail).toContain("downloads the application");
  });

  it("shows a step that did not pass as failed, with the reason", () => {
    const steps = setupSteps({ at: "token", failure: "memini: an active admin token exists" });

    expect(steps[2]).toMatchObject({ state: "failed", detail: "memini: an active admin token exists" });
  });

  it("shows every step as done when the setup is finished", () => {
    expect(setupSteps({ at: "finished", failure: null }).map((step) => step.state)).toEqual(["done", "done", "done"]);
  });
});

describe("parseSetupOutcome", () => {
  it("reads whether the setup worked and the connections, never a token", () => {
    const outcome = parseSetupOutcome({ ok: true, error: null, view });

    expect(outcome.ok).toBe(true);
    expect(outcome.view.connections[0]).toMatchObject({ kind: "local", hasToken: true });
  });

  it("throws a ShapeError when the answer has no connections", () => {
    expect(() => parseSetupOutcome({ ok: true })).toThrow(ShapeError);
  });

  it("recognizes the service's refusal because an admin token exists", () => {
    const refused = parseSetupOutcome({ ok: false, error: "memini: an active admin token exists (memini_Fake0Fak)", view });

    expect(tokenExists(refused)).toBe(true);
  });
});

describe("busyReason", () => {
  it("says why the buttons wait while the server restarts", () => {
    expect(busyReason("restart")).toBe("The server is restarting. The other actions wait until it is done.");
  });

  it("returns null when nothing runs", () => {
    expect(busyReason(null)).toBeNull();
  });
});

describe("runSetup", () => {
  it("starts the server, takes the token and returns the connections when everything passes", async () => {
    const result = await run({
      launcher_status: () => wire({}),
      launcher_change: () => ({ ok: true, error: null }),
      launcher_setup_token: () => ({ ok: true, error: null, view }),
    });

    expect(result.commands).toEqual(["launcher_status", "launcher_change", "launcher_setup_token"]);
    expect(result.progress.at(-1)).toEqual({ at: "finished", failure: null });
    expect(result.returned).toBe(true);
  });

  it("does not start a server that is running already", async () => {
    const result = await run({
      launcher_status: () => wire({ installed: true, running: true }),
      launcher_setup_token: () => ({ ok: true, error: null, view }),
    });

    expect(result.commands).toEqual(["launcher_status", "launcher_setup_token"]);
  });

  it("stops at Docker, with advice, and starts nothing when Docker is not there", async () => {
    const result = await run({
      launcher_status: () => wire({ docker: { available: false, error: "the docker command was not found" } }),
    });

    expect(result.commands).toEqual(["launcher_status"]);
    expect(result.progress.at(-1)).toEqual({ at: "docker", failure: "Docker is not installed" });
    expect(result.stopped[0]?.docker?.page).toBe(DOCKER_INSTALL_PAGE);
    expect(result.returned).toBe(false);
  });

  it("stops with the launcher's reason when the server does not start", async () => {
    const result = await run({
      launcher_status: () => wire({}),
      launcher_change: () => ({ ok: false, error: "docker compose failed with exit status 1" }),
    });

    expect(result.commands).toEqual(["launcher_status", "launcher_change"]);
    expect(result.progress.at(-1)).toEqual({ at: "server", failure: "docker compose failed with exit status 1" });
  });

  it("stops at the token and says so when the server has an admin token already", async () => {
    const result = await run({
      launcher_status: () => wire({ installed: true, running: true }),
      launcher_setup_token: () => ({ ok: false, error: "memini: an active admin token exists", view }),
    });

    expect(result.stopped).toEqual([{ docker: null, tokenExists: true }]);
    expect(result.returned).toBe(false);
  });

  it("asks for another admin token only when the user said so", async () => {
    const backend = createFakeBackend({
      answers: {
        launcher_status: () => wire({ installed: true, running: true }),
        launcher_setup_token: () => ({ ok: true, error: null, view }),
      },
    });

    await runSetup(backend, { force: true, onProgress: () => undefined, onStopped: () => undefined });

    expect(backend.calls.at(-1)).toEqual({ command: "launcher_setup_token", args: { force: true } });
  });
});
