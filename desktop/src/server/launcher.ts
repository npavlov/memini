// The launcher's answers as the Server screen shows them. Rust ran `memini-server`, checked the
// schema of its JSON and parsed it; this module reads that result and turns it into words.

import { readArray, readBoolean, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";
import type { Tone } from "../components";

export interface ServiceState {
  readonly name: string;
  readonly state: string;
  readonly health: string | null;
}

export interface LauncherStatus {
  readonly installed: boolean;
  readonly running: boolean;
  readonly apiUrl: string | null;
  readonly mcpUrl: string | null;
  readonly mcpEnabled: boolean | null;
  readonly image: string | null;
  readonly directory: string | null;
  readonly dockerAvailable: boolean;
  readonly dockerError: string | null;
  readonly services: readonly ServiceState[];
}

export interface LauncherResult {
  readonly ok: boolean;
  readonly error: string | null;
}

export const LAUNCHER_SOURCES = ["configured", "bundled", "installed"] as const;

/** Where the launcher that is run comes from: a path the user set, the app, or this computer. */
export type LauncherSource = (typeof LAUNCHER_SOURCES)[number];

export interface LauncherInfo {
  readonly configuredPath: string | null;
  readonly resolvedPath: string | null;
  readonly source: LauncherSource | null;
}

const SOURCE_WORDS: Readonly<Record<LauncherSource, string>> = {
  configured: "the path set here",
  bundled: "the app: it carries its own launcher",
  installed: "this computer: PATH and the package managers’ folders",
};

export function describeSource(source: LauncherSource | null): string {
  return source === null ? "nowhere: no launcher was found" : SOURCE_WORDS[source];
}

/** The changes the app may ask of the launcher. Rust maps each to a fixed argument vector. */
export type LauncherChange = "start" | "stop" | "restart" | "mcp_on" | "mcp_off";

export type ServerPhase =
  | "no_docker"
  | "not_installed"
  | "stopped"
  | "starting"
  | "unhealthy"
  | "running";

function shown(value: unknown): string | null {
  const text = readString(value);
  return text === null ? null : displayText(text);
}

function parseService(value: unknown): ServiceState {
  const service = readObject(value, "a service of the launcher's status");
  return {
    name: shown(service["name"]) ?? "",
    state: shown(service["state"]) ?? "",
    health: shown(service["health"]),
  };
}

export function parseLauncherStatus(value: unknown): LauncherStatus {
  const status = readObject(value, "the launcher's status");
  const docker = readObject(status["docker"], "the launcher's Docker report");
  return {
    installed: readBoolean(status["installed"]) ?? false,
    running: readBoolean(status["running"]) ?? false,
    apiUrl: shown(status["api_url"]),
    mcpUrl: shown(status["mcp_url"]),
    mcpEnabled: readBoolean(status["mcp_enabled"]),
    image: shown(status["image"]),
    directory: shown(status["directory"]),
    dockerAvailable: readBoolean(docker["available"]) ?? false,
    dockerError: shown(docker["error"]),
    services: readArray(status["services"]).map(parseService),
  };
}

export function parseLauncherResult(value: unknown): LauncherResult {
  const result = readObject(value, "the launcher's result");
  return { ok: readBoolean(result["ok"]) ?? false, error: shown(result["error"]) };
}

export function parseLauncherInfo(value: unknown): LauncherInfo {
  const info = readObject(value, "the launcher's location");
  return {
    configuredPath: readString(info["configured_path"]),
    resolvedPath: readString(info["resolved_path"]),
    source: LAUNCHER_SOURCES.find((source) => source === info["source"]) ?? null,
  };
}

function isUp(service: ServiceState): boolean {
  return service.state.toLowerCase() === "running";
}

/** Where the local server stands, from the least to the most ready. */
export function serverPhase(status: LauncherStatus): ServerPhase {
  if (!status.dockerAvailable) {
    return "no_docker";
  }
  if (!status.installed) {
    return "not_installed";
  }
  if (!status.running) {
    return "stopped";
  }
  if (status.services.some((service) => service.health?.toLowerCase() === "unhealthy")) {
    return "unhealthy";
  }
  const settled = status.services.every(
    (service) => isUp(service) && service.health?.toLowerCase() !== "starting",
  );
  return settled ? "running" : "starting";
}

export interface PhaseSummary {
  readonly tone: Tone;
  readonly title: string;
  readonly text: string;
}

const PHASES: Readonly<Record<ServerPhase, PhaseSummary>> = {
  no_docker: {
    tone: "bad",
    title: "Docker is not available",
    text: "Memini runs in Docker. Install Docker with Compose 2.24 or later, start it, and refresh.",
  },
  not_installed: {
    tone: "info",
    title: "No server on this computer yet",
    text: "Start creates it: private settings, then the application, the database and the search model are downloaded.",
  },
  stopped: {
    tone: "idle",
    title: "The server is stopped",
    text: "Its settings and data are kept. Start brings the same installation back.",
  },
  starting: {
    tone: "warn",
    title: "The server is starting",
    text: "Some services are not up yet. Refresh in a moment.",
  },
  unhealthy: {
    tone: "bad",
    title: "A service is unhealthy",
    text: "The server runs, but a service fails its health check. See the services below.",
  },
  running: {
    tone: "good",
    title: "The server is running",
    text: "Every service is up.",
  },
};

export function describePhase(phase: ServerPhase): PhaseSummary {
  return PHASES[phase];
}

export function serviceTone(service: ServiceState): Tone {
  const health = service.health?.toLowerCase() ?? "";
  if (health === "unhealthy") {
    return "bad";
  }
  if (!isUp(service)) {
    return "idle";
  }
  return health === "starting" ? "warn" : "good";
}

export interface InstallStep {
  readonly title: string;
  readonly text: string;
}

const SOURCE = "https://github.com/npavlov/memini.git";

/** The install commands of the README for the operating system the app runs on. */
export function installSteps(os: string): readonly InstallStep[] {
  if (os === "windows") {
    return [
      {
        title: "Windows, in PowerShell with Scoop",
        text: `scoop bucket add memini ${SOURCE}\nscoop install memini/memini-server`,
      },
    ];
  }
  return [
    {
      title: os === "macos" ? "macOS, with Homebrew" : "Linux, with Homebrew",
      text: `brew tap npavlov/memini ${SOURCE}\nbrew install npavlov/memini/memini-server`,
    },
  ];
}
