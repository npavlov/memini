// The state of the selected connection, for the bar at the bottom of the window: whether the
// server answers, its version, whether the base is current, whether MCP is on. Each part is asked
// separately, so one that fails does not hide the others. A connection whose token is missing or
// refused is never shown as healthy: the bar names that first.

import type { TokenCheck } from "../connections/connection-check";
import type { Connection } from "../connections/connections";
import { checkConnection } from "../connections/connections-api";
import { toAppError } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";
import type { Backend } from "../ipc/backend";
import { probeEndpoint } from "../mcp/mcp-endpoint";
import type { EndpointState } from "../mcp/mcp-endpoint";
import type { Tone } from "../components";
import { totals } from "./freshness";
import { readServiceStatus } from "./status";
import type { ServiceStatus } from "./status";

export interface ConnectionHealth {
  /** The server answered the request for its version. */
  readonly answers: boolean;
  /** Why it did not answer. */
  readonly problem: AppError | null;
  readonly version: string | null;
  readonly ready: boolean | null;
  /** What is known about the token; null when the check itself could not be made. */
  readonly token: TokenCheck | null;
  /** The scope the token lacks, when the server said so. */
  readonly lacking: string | null;
  /** Whether the MCP endpoint is there; null when the server could not be asked. */
  readonly mcp: EndpointState | null;
  /** The status of the base, when the token may read it. */
  readonly status: ServiceStatus | null;
  /** Why the status is missing although the token was accepted. */
  readonly statusProblem: AppError | null;
}

type Attempt<T> = { readonly value: T; readonly error: null } | { readonly value: null; readonly error: AppError };

async function attempt<T>(work: () => Promise<T>): Promise<Attempt<T>> {
  try {
    return { value: await work(), error: null };
  } catch (error) {
    return { value: null, error: toAppError(error) };
  }
}

const SILENT: ConnectionHealth = {
  answers: false,
  problem: null,
  version: null,
  ready: null,
  token: null,
  lacking: null,
  mcp: null,
  status: null,
  statusProblem: null,
};

/** Asks the connection's server for everything the bar shows. Never rejects. */
export async function readConnectionHealth(
  backend: Backend,
  connection: Connection,
): Promise<ConnectionHealth> {
  const check = await attempt(() => checkConnection(backend, connection.id));
  if (check.value === null) {
    return { ...SILENT, problem: check.error };
  }
  if (check.value.unreachable !== null) {
    // Whether a token is stored is known without the server.
    return { ...SILENT, problem: check.value.unreachable, token: check.value.token };
  }
  const readable = check.value.token === "accepted";
  const [mcp, status] = await Promise.all([
    attempt(() => probeEndpoint(backend, connection.id)),
    readable ? attempt(() => readServiceStatus(backend, connection.id)) : null,
  ]);
  return {
    answers: true,
    problem: null,
    version: check.value.version,
    ready: check.value.ready,
    token: check.value.token,
    lacking: check.value.lacking,
    mcp: mcp.value,
    status: status?.value ?? null,
    statusProblem: status?.error ?? null,
  };
}

/** One state of the bar: a few words, coloured by their tone. */
export interface BarItem {
  readonly key: "server" | "version" | "token" | "base" | "mcp";
  readonly tone: Tone;
  readonly label: string;
}

/** What the bar says about the token when it keeps the app from the data; null when it does not. */
function tokenItem(health: ConnectionHealth): BarItem | null {
  switch (health.token) {
    case "missing":
      return { key: "token", tone: "warn", label: "No token" };
    case "refused":
      return { key: "token", tone: "bad", label: "Token refused" };
    case "lacks_scope":
      return { key: "token", tone: "warn", label: `Token lacks the ${health.lacking ?? "read"} scope` };
    default:
      return null;
  }
}

function serverItem(health: ConnectionHealth): BarItem {
  if (!health.answers) {
    return { key: "server", tone: "bad", label: "server does not answer" };
  }
  if (health.ready === false) {
    return { key: "server", tone: "warn", label: "server answers, not ready" };
  }
  // Green is for a connection that works: a server that answers a token it does not accept is not one.
  return { key: "server", tone: tokenItem(health) === null ? "good" : "idle", label: "server answers" };
}

function baseItem(health: ConnectionHealth): BarItem {
  if (health.status === null) {
    return { key: "base", tone: "warn", label: "base unknown" };
  }
  if (health.status.current) {
    return { key: "base", tone: "good", label: "base current" };
  }
  const counted = totals(health.status);
  const detail = counted.behind > 0 ? `: ${String(counted.behind)} of ${String(counted.streams)} streams` : "";
  return { key: "base", tone: "bad", label: `base behind${detail}` };
}

function mcpItem(mcp: EndpointState | null): BarItem {
  if (mcp === "on") {
    return { key: "mcp", tone: "good", label: "MCP on" };
  }
  return mcp === "off"
    ? { key: "mcp", tone: "idle", label: "MCP off" }
    : { key: "mcp", tone: "warn", label: "MCP unknown" };
}

/**
 * What the bar says about a connection. A server that does not answer has nothing else to say,
 * except that no token is stored. A token that is missing, refused or short of a scope stands
 * where the state of the base would: without it the base cannot be read.
 */
export function barItems(health: ConnectionHealth): readonly BarItem[] {
  const token = tokenItem(health);
  if (!health.answers) {
    return token === null ? [serverItem(health)] : [serverItem(health), token];
  }
  const version: BarItem = {
    key: "version",
    tone: "info",
    label: health.version === null ? "version unknown" : `version ${health.version}`,
  };
  return [serverItem(health), version, token ?? baseItem(health), mcpItem(health.mcp)];
}
