// Whether a server's MCP endpoint is on, and where it is. For the local server the launcher
// knows; for any server the Rust side can ask the endpoint itself, without a token.

import { isLoopbackAddress } from "../connections/connections";
import type { Connection } from "../connections/connections";
import type { Backend } from "../ipc/backend";
import type { LauncherStatus } from "../server/launcher";
import type { Tone } from "../components";
import { mcpAddress, mcpAddressOf } from "./mcp-clients";

export const ENDPOINT_STATES = ["on", "off", "unknown"] as const;

export type EndpointState = (typeof ENDPOINT_STATES)[number];

/** Asks the connection's server whether its MCP endpoint answers. */
export async function probeEndpoint(backend: Backend, id: string): Promise<EndpointState> {
  const answer = await backend.invoke("mcp_probe", { id });
  return ENDPOINT_STATES.find((state) => state === answer) ?? "unknown";
}

/**
 * The MCP address of a connection. The launcher's own answer is used for the local server while
 * it names an address on this computer; otherwise the address follows from the connection's.
 */
export function endpointAddress(
  connection: Connection,
  launcher: LauncherStatus | null,
): string | null {
  const reported = connection.kind === "local" ? (launcher?.mcpUrl ?? null) : null;
  const local = reported !== null && isLoopbackAddress(reported) ? mcpAddress(reported) : null;
  return local ?? mcpAddressOf(connection.url);
}

/** What the launcher says about the local endpoint; null when it does not say. */
export function launcherEndpointState(launcher: LauncherStatus | null): EndpointState | null {
  if (launcher === null || launcher.mcpEnabled === null) {
    return null;
  }
  return launcher.mcpEnabled ? "on" : "off";
}

export interface EndpointSummary {
  readonly tone: Tone;
  readonly label: string;
  readonly text: string;
}

const SUMMARIES: Readonly<Record<EndpointState, EndpointSummary>> = {
  on: {
    tone: "good",
    label: "On",
    text: "Agents can connect with a read token.",
  },
  off: {
    tone: "idle",
    label: "Off",
    text: "The server does not serve MCP. The HTTP API and the CLI still work.",
  },
  unknown: {
    tone: "warn",
    label: "Unknown",
    text: "The server did not say whether its MCP endpoint is on.",
  },
};

export function describeEndpoint(state: EndpointState): EndpointSummary {
  return SUMMARIES[state];
}

/**
 * Why the endpoint cannot be switched from this app; null when it can. The switch is the
 * launcher's: it works for the server on this computer, while the launcher answers.
 */
export function switchUnavailable(connection: Connection, launcher: LauncherStatus | null): string | null {
  if (connection.kind !== "local") {
    return "The switch is on the computer that runs the server: its operator sets it.";
  }
  return launcher === null ? "The switch needs the memini-server launcher; see the Server screen." : null;
}
