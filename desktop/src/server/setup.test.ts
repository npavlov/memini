import { describe, expect, it } from "vitest";

import type { Connection } from "../connections/connections";
import { createFakeBackend } from "../ipc/fake-backend";
import type { FakeAnswer } from "../ipc/fake-backend";
import { ShapeError } from "../json/read-json";
import type { LauncherStatus } from "./launcher";
import { removeServer } from "./launcher-api";
import {
  DOCKER_INSTALL_PAGE,
  DOWNLOADING,
  NOT_STARTED,
  busyReason,
  dockerAdvice,
  localSetup,
  parseSetupOutcome,
  setupSteps,
  tokenExists,
} from "./setup";
import type { SetupProgress } from "./setup";
import { NOT_STOPPED, runSetup } from "./setup-run";
import type { OllamaChoice, Stopped } from "./setup-run";

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

/** Ollama on this computer as Rust answers: not a Mac unless the changes say so. */
function host(changes: Record<string, unknown>): unknown {
  return {
    supported: false,
    found: false,
    version: null,
    model: "qwen3-embedding:0.6b",
    has_model: false,
    brew: false,
    pull: null,
    installing: false,
    install: null,
    ...changes,
  };
}

const NOT_A_MAC = (): unknown => host({});

async function run(
  answers: Readonly<Record<string, FakeAnswer>>,
  ollama: OllamaChoice = "ask",
): Promise<Run> {
  const backend = createFakeBackend({ answers: { host_model_status: NOT_A_MAC, ...answers } });
  const progress: SetupProgress[] = [];
  const stopped: Stopped[] = [];
  const returned = await runSetup(backend, {
    force: false,
    ollama,
    onProgress: (step) => progress.push(step),
    onStopped: (found) => stopped.push(found),
    // No real second between two looks, but a turn of the event loop, so timers still run.
    wait: async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    },
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

describe("localSetup", () => {
  const none = status({ installed: false, running: false });
  it.each([
    ["no server is installed and nothing is connected", none, null, "setup"],
    ["no server is installed and the connection has no token", none, { ...local, hasToken: false }, "setup"],
    ["a server set up another way answers the connection", none, local, "foreign"],
    ["this computer is not a connection", status({}), null, "setup"],
    ["the connection has no token", status({}), { ...local, hasToken: false }, "setup"],
    ["the server is installed and the app holds its token", status({}), local, "done"],
    ["the server is only stopped", status({ running: false }), local, "done"],
  ] as const)("offers what fits when %s", (_what, given, connection, expected) => {
    expect(localSetup(given, connection)).toBe(expected);
  });
});

describe("setupSteps", () => {
  it("shows every step as waiting before the start", () => {
    expect(setupSteps(NOT_STARTED).map((step) => step.state)).toEqual(["waiting", "waiting", "waiting", "waiting"]);
  });

  it("shows the steps before the current one as done and says the start is downloading", () => {
    const steps = setupSteps({ at: "server", failure: null });

    expect(steps.map((step) => step.state)).toEqual(["done", "done", "running", "waiting"]);
    expect(steps[2]?.detail).toBe(DOWNLOADING);
  });

  it("points to the services once the launcher lists any", () => {
    const steps = setupSteps({ at: "server", failure: null }, true);

    expect(steps[2]?.detail).toContain("the list below shows how far each one is");
  });

  it("shows what the running step reports of itself, such as a download", () => {
    const steps = setupSteps({ at: "model", failure: null, note: "Downloading: 45%" });

    expect(steps[1]).toMatchObject({ title: "Prepare the search model", state: "running", detail: "Downloading: 45%" });
  });

  it("shows a step that did not pass as failed, with the reason", () => {
    const steps = setupSteps({ at: "token", failure: "memini: an active admin token exists" });

    expect(steps[3]).toMatchObject({ state: "failed", detail: "memini: an active admin token exists" });
  });

  it("shows every step as done when the setup is finished", () => {
    expect(setupSteps({ at: "finished", failure: null }).map((step) => step.state)).toEqual([
      "done",
      "done",
      "done",
      "done",
    ]);
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

    expect(result.commands).toEqual([
      "launcher_status",
      "host_model_status",
      "launcher_change",
      "launcher_setup_token",
    ]);
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

    expect(result.commands).toEqual(["launcher_status", "host_model_status", "launcher_change"]);
    expect(result.progress.at(-1)).toEqual({ at: "server", failure: "docker compose failed with exit status 1" });
  });

  it("stops at the token and says so when the server has an admin token already", async () => {
    const result = await run({
      launcher_status: () => wire({ installed: true, running: true }),
      launcher_setup_token: () => ({ ok: false, error: "memini: an active admin token exists", view }),
    });

    expect(result.stopped).toEqual([{ ...NOT_STOPPED, tokenExists: true }]);
    expect(result.returned).toBe(false);
  });

  it("asks for another admin token only when the user said so", async () => {
    const backend = createFakeBackend({
      answers: {
        launcher_status: () => wire({ installed: true, running: true }),
        launcher_setup_token: () => ({ ok: true, error: null, view }),
      },
    });

    await runSetup(backend, {
      force: true,
      ollama: "ask",
      onProgress: () => undefined,
      onStopped: () => undefined,
    });

    expect(backend.calls.at(-1)).toEqual({ command: "launcher_setup_token", args: { force: true } });
  });
});

describe("runSetup on a Mac", () => {
  const start = {
    launcher_status: () => wire({}),
    launcher_change: () => ({ ok: true, error: null }),
    launcher_setup_token: () => ({ ok: true, error: null, view }),
  };

  it("goes on at once when Ollama on the Mac has the model", async () => {
    const result = await run({
      ...start,
      host_model_status: () => host({ supported: true, found: true, has_model: true }),
    });

    expect(result.commands).toEqual([
      "launcher_status",
      "host_model_status",
      "launcher_change",
      "launcher_setup_token",
    ]);
    expect(result.returned).toBe(true);
  });

  it("downloads the model to Ollama on the Mac and shows how far it is", async () => {
    let looks = 0;
    const result = await run({
      ...start,
      host_model_status: () => {
        looks += 1;
        return host({
          supported: true,
          found: true,
          has_model: false,
          pull: { status: "pulling", completed: 319_500_000, total: 639_000_000, done: false, error: null },
        });
      },
      host_model_pull: async () => {
        await new Promise((resolve) => setTimeout(resolve, 5));
        return { status: "success", completed: 639_000_000, total: 639_000_000, done: true, error: null };
      },
    });

    expect(looks).toBeGreaterThan(1);
    expect(result.progress.some((step) => step.note?.includes("50%, 320 of 639 MB"))).toBe(true);
    expect(result.progress).toContainEqual(
      expect.objectContaining({ download: { completed: 319_500_000, total: 639_000_000 } }),
    );
    expect(result.commands).toContain("host_model_pull");
    expect(result.returned).toBe(true);
  });

  it("stops with Ollama's reason when the download fails", async () => {
    const result = await run({
      ...start,
      host_model_status: () => host({ supported: true, found: true }),
      host_model_pull: () => ({ status: "", completed: 0, total: 0, done: true, error: "no space left" }),
    });

    expect(result.progress.at(-1)).toEqual({ at: "model", failure: "no space left" });
    expect(result.commands).not.toContain("launcher_change");
  });

  it("stops and asks when no Ollama answers on the Mac, saying whether Homebrew can install it", async () => {
    const result = await run({ ...start, host_model_status: () => host({ supported: true, brew: true }) });

    expect(result.stopped).toEqual([{ ...NOT_STOPPED, ollama: { brew: true, install: null } }]);
    expect(result.progress.at(-1)?.at).toBe("model");
    expect(result.commands).not.toContain("launcher_change");
    expect(result.commands).not.toContain("host_ollama_install");
  });

  it("goes on with the model in Docker when the user said so", async () => {
    const result = await run({ ...start, host_model_status: () => host({ supported: true }) }, "skip");

    expect(result.commands).toContain("launcher_change");
    expect(result.returned).toBe(true);
  });

  it("installs Ollama with Homebrew when the user said so, then downloads the model", async () => {
    let installed = false;
    const result = await run(
      {
        ...start,
        host_model_status: () => host({ supported: true, brew: true, found: installed }),
        host_ollama_install: () => {
          installed = true;
          return { ok: true, error: null, tail: ["==> Pouring ollama"] };
        },
        host_model_pull: () => ({ status: "success", completed: 1, total: 1, done: true, error: null }),
      },
      "install",
    );

    expect(result.commands.slice(0, 4)).toEqual([
      "launcher_status",
      "host_model_status",
      "host_ollama_install",
      "host_model_status",
    ]);
    expect(result.commands).toContain("host_model_pull");
    expect(result.returned).toBe(true);
  });

  it("shows what Homebrew said when the installation failed", async () => {
    const result = await run(
      {
        ...start,
        host_model_status: () => host({ supported: true, brew: true }),
        host_ollama_install: () => ({ ok: false, error: "brew install ollama failed.", tail: ["Error: no network"] }),
      },
      "install",
    );

    expect(result.stopped).toEqual([
      { ...NOT_STOPPED, ollama: { brew: true, install: { ok: false, error: "brew install ollama failed.", tail: ["Error: no network"] } } },
    ]);
    expect(result.progress.at(-1)).toEqual({ at: "model", failure: "brew install ollama failed." });
  });

  it("leaves an installed server's model where it is", async () => {
    const result = await run({
      launcher_status: () => wire({ installed: true, running: true }),
      launcher_setup_token: () => ({ ok: true, error: null, view }),
    });

    expect(result.commands).toEqual(["launcher_status", "launcher_setup_token"]);
  });
});

describe("removeServer", () => {
  it("asks Rust to remove the server and returns what is left of the connections", async () => {
    const backend = createFakeBackend({
      answers: { launcher_uninstall: () => ({ ok: true, error: null, view }) },
    });

    const outcome = await removeServer(backend);

    expect(backend.calls).toEqual([{ command: "launcher_uninstall", args: {} }]);
    expect(outcome.ok).toBe(true);
    expect(outcome.view.connections.map((connection) => connection.id)).toEqual(["c1"]);
  });

  it("gives the launcher's reason when the removal failed", async () => {
    const backend = createFakeBackend({
      answers: {
        launcher_uninstall: () => ({ ok: false, error: "docker compose failed with exit status 1", view }),
      },
    });

    const outcome = await removeServer(backend);

    expect(outcome).toMatchObject({ ok: false, error: "docker compose failed with exit status 1" });
  });
});
