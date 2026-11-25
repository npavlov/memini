// The first setup of the server on this computer, as the Server screen shows it: is Docker
// there, is the server installed and running, does the app hold its admin token. The launcher
// does the work; this module decides what to say and which step is next.

import type { Connection, ConnectionsView } from "../connections/connections";
import { parseConnectionsView } from "../connections/connections";
import { readBoolean, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";
import type { LauncherChange, LauncherStatus } from "./launcher";

/** Where Docker says how to install it. Shown as text to copy: the app opens no link. */
export const DOCKER_INSTALL_PAGE = "https://docs.docker.com/get-started/get-docker/";

export interface DockerAdvice {
  readonly title: string;
  readonly text: string;
  /** The page to read, when installing is what is needed. */
  readonly page: string | null;
}

/** What to do about Docker, or null when it is there. The launcher's own words tell the cases apart. */
export function dockerAdvice(status: LauncherStatus): DockerAdvice | null {
  if (status.dockerAvailable) {
    return null;
  }
  if ((status.dockerError ?? "").includes("not found")) {
    return {
      title: "Docker is not installed",
      text: "Memini runs in Docker. Install Docker Desktop on macOS or Windows, or Docker Engine on Linux, with Compose 2.24 or later. Then start it and set up again.",
      page: DOCKER_INSTALL_PAGE,
    };
  }
  return {
    title: "Docker is not running",
    text: "Docker is installed but its engine does not answer. Start Docker Desktop, or the Docker service on Linux, wait until it says it is running, and set up again.",
    page: null,
  };
}

/**
 * What the Server screen offers for this computer:
 * - `setup`: the first setup has something to do, as no server yet or no token for it in the app;
 * - `foreign`: the local connection holds a token, but the launcher manages no server. That
 *   server was set up another way; a setup would install a second one and give the connection a
 *   token of the wrong server, so none is offered;
 * - `done`: the launcher manages the server and the app holds its token.
 */
export type LocalSetup = "setup" | "foreign" | "done";

export function localSetup(status: LauncherStatus, local: Connection | null): LocalSetup {
  if (!status.installed) {
    return local !== null && local.hasToken ? "foreign" : "setup";
  }
  return local === null || !local.hasToken ? "setup" : "done";
}

export const SETUP_STEPS = ["docker", "server", "token"] as const;

export type SetupStepId = (typeof SETUP_STEPS)[number];

export type StepState = "waiting" | "running" | "done" | "failed";

/** How far the setup is: the step it is at, and why it stopped if it did. */
export interface SetupProgress {
  /** The step that runs or failed; null before the start; "finished" after the last. */
  readonly at: SetupStepId | "finished" | null;
  readonly failure: string | null;
}

export const NOT_STARTED: SetupProgress = { at: null, failure: null };

export interface SetupStep {
  readonly id: SetupStepId;
  readonly title: string;
  readonly state: StepState;
  readonly detail: string | null;
}

const TITLES: Readonly<Record<SetupStepId, string>> = {
  docker: "Check Docker",
  server: "Install and start the server",
  token: "Create the first admin token and connect",
};

const RUNNING: Readonly<Record<SetupStepId, string>> = {
  docker: "Asking Docker…",
  server: "The first start downloads the application, the database and the search model. This can take a long time; the services below show how far it is.",
  token: "The token goes from the server straight to the system keychain.",
};

function stateOf(step: SetupStepId, progress: SetupProgress): StepState {
  if (progress.at === null) {
    return "waiting";
  }
  if (progress.at === "finished") {
    return "done";
  }
  const position = SETUP_STEPS.indexOf(step);
  const current = SETUP_STEPS.indexOf(progress.at);
  if (position < current) {
    return "done";
  }
  if (position > current) {
    return "waiting";
  }
  return progress.failure === null ? "running" : "failed";
}

/** The three steps with their state, for the list the setup shows. */
export function setupSteps(progress: SetupProgress): readonly SetupStep[] {
  return SETUP_STEPS.map((id) => {
    const state = stateOf(id, progress);
    const failed = state === "failed" ? progress.failure : null;
    return { id, title: TITLES[id], state, detail: state === "running" ? RUNNING[id] : failed };
  });
}

/** What the last step of the setup answered. It never holds the token. */
export interface SetupOutcome {
  readonly ok: boolean;
  readonly error: string | null;
  readonly view: ConnectionsView;
}

export function parseSetupOutcome(value: unknown): SetupOutcome {
  const outcome = readObject(value, "the result of the setup");
  const error = readString(outcome["error"]);
  return {
    ok: readBoolean(outcome["ok"]) ?? false,
    error: error === null ? null : displayText(error),
    view: parseConnectionsView(outcome["view"]),
  };
}

/** Whether the service refused the token because the server has an admin token already. */
export function tokenExists(outcome: SetupOutcome): boolean {
  return !outcome.ok && (outcome.error ?? "").includes("admin token exists");
}

/** What can run on the Server screen: a change, the first setup or the removal. */
export type ServerWork = LauncherChange | "setup" | "uninstall";

const WORKING: Readonly<Record<ServerWork, string>> = {
  start: "The server is starting.",
  stop: "The server is stopping.",
  restart: "The server is restarting.",
  mcp_on: "MCP is being switched on.",
  mcp_off: "MCP is being switched off.",
  setup: "The setup is running.",
  uninstall: "The server is being removed.",
};

/** Why the buttons are disabled while something runs; null when nothing does. */
export function busyReason(working: ServerWork | null): string | null {
  return working === null ? null : `${WORKING[working]} The other actions wait until it is done.`;
}
