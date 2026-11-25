import type { ReactElement } from "react";

import { DataTable, Fact, Facts, Mono, Panel, Pill, Text } from "../components";
import { serviceTone } from "./launcher";
import type { LauncherStatus, ServiceState } from "./launcher";

export interface ServerFactsProps {
  readonly status: LauncherStatus;
}

function mcpWords(enabled: boolean | null): string {
  if (enabled === null) {
    return "the launcher did not say";
  }
  return enabled ? "on" : "off";
}

interface LiteralProps {
  readonly value: string | null;
}

/** An address or a path the launcher reported, or a dash when it reported none. */
function Literal({ value }: LiteralProps): ReactElement {
  return value === null ? <>—</> : <Mono>{value}</Mono>;
}

/** What the launcher reports about the installation, as text, and the state of each service. */
export function ServerFacts({ status }: ServerFactsProps): ReactElement {
  return (
    <Panel title="Installation and services">
      <Facts>
        <Fact name="Docker">
          <Pill tone={status.dockerAvailable ? "good" : "bad"}>
            {status.dockerAvailable ? "available" : "not available"}
          </Pill>
        </Fact>
        <Fact name="Server">
          <Pill tone={status.installed ? "good" : "idle"}>
            {status.installed ? "installed" : "not installed"}
          </Pill>
        </Fact>
        <Fact name="Address">
          <Literal value={status.apiUrl} />
        </Fact>
        <Fact name="MCP address">
          <Literal value={status.mcpUrl} />
        </Fact>
        <Fact name="MCP endpoint">{mcpWords(status.mcpEnabled)}</Fact>
        <Fact name="Image">
          <Literal value={status.image} />
        </Fact>
        <Fact name="Settings folder">
          <Literal value={status.directory} />
        </Fact>
      </Facts>
      <ServicesTable services={status.services} />
    </Panel>
  );
}

interface ServicesTableProps {
  readonly services: readonly ServiceState[];
}

function ServicesTable({ services }: ServicesTableProps): ReactElement {
  if (services.length === 0) {
    return <Text tone="muted">No service exists yet. They are created by the first start.</Text>;
  }
  return (
    <DataTable columns={["Service", "State", "Health"]}>
      {services.map((service, position) => (
        <tr key={`${String(position)}:${service.name}`}>
          <th scope="row">{service.name}</th>
          <td>
            <Pill tone={serviceTone(service)}>{service.state === "" ? "unknown" : service.state}</Pill>
          </td>
          <td>{service.health ?? "no health check"}</td>
        </tr>
      ))}
    </DataTable>
  );
}
