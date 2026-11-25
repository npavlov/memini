import type { ReactElement } from "react";

import { DataTable } from "../ui/data-table";
import { Fact, Panel } from "../ui/panel";
import { Pill } from "../ui/pill";
import { serviceTone } from "./launcher";
import type { LauncherStatus, ServiceState } from "./launcher";

export interface ServerFactsProps {
  readonly status: LauncherStatus;
}

const NOTHING = "—";

function mcpWords(enabled: boolean | null): string {
  if (enabled === null) {
    return "the launcher did not say";
  }
  return enabled ? "on" : "off";
}

/** What the launcher reports about the installation, as text, and the state of each service. */
export function ServerFacts({ status }: ServerFactsProps): ReactElement {
  return (
    <Panel title="Installation and services">
      <dl className="facts">
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
        <Fact name="Address">{status.apiUrl ?? NOTHING}</Fact>
        <Fact name="MCP address">{status.mcpUrl ?? NOTHING}</Fact>
        <Fact name="MCP endpoint">{mcpWords(status.mcpEnabled)}</Fact>
        <Fact name="Image">{status.image ?? NOTHING}</Fact>
        <Fact name="Settings folder">{status.directory ?? NOTHING}</Fact>
      </dl>
      <ServicesTable services={status.services} />
    </Panel>
  );
}

interface ServicesTableProps {
  readonly services: readonly ServiceState[];
}

function ServicesTable({ services }: ServicesTableProps): ReactElement {
  if (services.length === 0) {
    return <p className="muted">No service exists yet. They are created by the first start.</p>;
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
