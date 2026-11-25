import { useCallback } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { AppError } from "../ipc/app-error";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import type { LauncherStatus } from "../server/launcher";
import { readLauncherStatus } from "../server/launcher-api";
import { movedLocalAddress } from "../server/local-address";
import { CopyButton } from "../ui/copy-button";
import { ErrorNotice, Notice } from "../ui/notice";
import { Fact, Panel, Screen } from "../ui/panel";
import { Pill } from "../ui/pill";
import { useLoad } from "../ui/use-load";
import { ClientGuides } from "./client-guides";
import {
  describeEndpoint,
  endpointAddress,
  launcherEndpointState,
  probeEndpoint,
} from "./mcp-endpoint";
import type { EndpointState } from "./mcp-endpoint";
import { McpSwitch } from "./mcp-switch";

export interface McpScreenProps {
  readonly connection: Connection;
}

interface Endpoint {
  /** The launcher's status, for the local server when the launcher answers. */
  readonly launcher: LauncherStatus | null;
  readonly state: EndpointState;
}

/** The launcher's status, or null when there is no launcher to ask: the endpoint is asked then. */
async function readLauncher(backend: Backend, connection: Connection): Promise<LauncherStatus | null> {
  if (connection.kind !== "local") {
    return null;
  }
  try {
    return await readLauncherStatus(backend);
  } catch (error) {
    if (error instanceof AppError) {
      return null;
    }
    throw error;
  }
}

async function readEndpoint(backend: Backend, connection: Connection): Promise<Endpoint> {
  const launcher = await readLauncher(backend, connection);
  const state = launcherEndpointState(launcher) ?? (await probeEndpoint(backend, connection.id));
  return { launcher, state };
}

export function McpScreen({ connection }: McpScreenProps): ReactElement {
  const backend = useBackend();
  const endpoint = useLoad(
    useCallback(() => readEndpoint(backend, connection), [backend, connection]),
  );
  const loaded = endpoint.state.phase === "ready" ? endpoint.state.data : null;
  const address = endpointAddress(connection, loaded?.launcher ?? null);
  return (
    <Screen
      title="MCP"
      lead="The address agents connect to, whether it is on, and configuration to paste into a client."
    >
      <Panel
        title="Endpoint"
        actions={
          <button type="button" className="button" disabled={endpoint.busy} onClick={endpoint.reload}>
            {endpoint.busy ? "Checking…" : "Check again"}
          </button>
        }
      >
        <dl className="facts">
          <Fact name="Address">
            {address ?? "The connection’s address is not valid."}
            {address === null ? null : <CopyButton text={address} label="Copy address" />}
          </Fact>
        </dl>
        {endpoint.state.phase === "loading" ? <p className="muted">Asking the server…</p> : null}
        {endpoint.state.phase === "failed" ? <ErrorNotice error={endpoint.state.error} /> : null}
        {loaded === null ? null : (
          <EndpointReport connection={connection} endpoint={loaded} onChanged={endpoint.reload} />
        )}
      </Panel>
      {address === null ? null : <ClientGuides address={address} />}
    </Screen>
  );
}

interface EndpointReportProps {
  readonly connection: Connection;
  readonly endpoint: Endpoint;
  readonly onChanged: () => void;
}

function EndpointReport({ connection, endpoint, onChanged }: EndpointReportProps): ReactElement {
  const summary = describeEndpoint(endpoint.state);
  const switchable = endpoint.launcher !== null && endpoint.state !== "unknown";
  const moved = movedLocalAddress(connection, endpoint.launcher);
  return (
    <>
      {moved === null ? null : (
        <Notice tone="warn" title="The server answers at another address now">
          <p>
            The launcher reports {moved}; this connection uses {connection.url}. Update the
            connection on the Server screen, then Status and Tokens reach the server again.
          </p>
        </Notice>
      )}
      <Notice tone={summary.tone}>
        <p>
          <Pill tone={summary.tone}>{summary.label}</Pill> {summary.text}
        </p>
        {endpoint.launcher !== null && !endpoint.launcher.running ? (
          <p className="muted small">The server is not running; this is its setting.</p>
        ) : null}
      </Notice>
      {switchable ? (
        <McpSwitch on={endpoint.state === "on"} onChanged={onChanged} />
      ) : (
        <p className="muted">
          {connection.kind === "local"
            ? "The switch needs the memini-server launcher; see the Server screen."
            : "The switch is on the computer that runs the server: its operator sets it."}
        </p>
      )}
    </>
  );
}
