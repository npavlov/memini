import type { ReactElement } from "react";

import { ErrorNotice, Fact, Facts, Notice, Panel, Pill, Text } from "../components";
import { describeTokenCheck } from "../connections/connection-check";
import { countNoun } from "../text/count-noun";
import { formatInstant } from "../text/format-time";
import type { ConnectionHealth } from "./connection-health";
import { indexVerdict, totals } from "./freshness";
import type { ServiceStatus } from "./status";
import { WorkspacePanel } from "./workspace-panel";

export interface StatusDetailsProps {
  readonly health: ConnectionHealth;
}

/** What the bar opens: the base as a whole, then each workspace with its streams. */
export function StatusDetails({ health }: StatusDetailsProps): ReactElement {
  if (!health.answers) {
    return <ErrorNotice error={health.problem} />;
  }
  if (health.status === null) {
    return <WithoutStatus health={health} />;
  }
  return (
    <>
      <Panel title="The base">
        <Summary status={health.status} />
      </Panel>
      {health.status.workspaces.map((workspace, position) => (
        <WorkspacePanel key={`${String(position)}:${workspace.name}`} workspace={workspace} />
      ))}
    </>
  );
}

/** The server answers, but the base cannot be read: say why, in the token's terms. */
function WithoutStatus({ health }: StatusDetailsProps): ReactElement {
  if (health.statusProblem !== null) {
    return <ErrorNotice error={health.statusProblem} />;
  }
  const finding = describeTokenCheck({
    version: health.version ?? "",
    ready: health.ready,
    token: health.token ?? "unknown",
    workspaces: null,
    detail: null,
  });
  return (
    <Notice tone={finding.tone} title={finding.text}>
      <Text>The freshness of the base is read with a token that has the read scope.</Text>
    </Notice>
  );
}

interface SummaryProps {
  readonly status: ServiceStatus;
}

function schemaWords(status: ServiceStatus): string {
  if (!status.databaseReachable) {
    return "not reachable";
  }
  const applied = status.schemaVersion === null ? "unknown" : String(status.schemaVersion);
  const expected =
    status.expectedSchemaVersion === null ? "unknown" : String(status.expectedSchemaVersion);
  return applied === expected ? `schema ${applied}` : `schema ${applied}, expected ${expected}`;
}

function Summary({ status }: SummaryProps): ReactElement {
  const counts = totals(status);
  const index = indexVerdict(status.indexState);
  return (
    <>
      <Notice
        tone={status.current ? "good" : "bad"}
        title={status.current ? "The base is current" : "The base is not current"}
      >
        <Text>
          {countNoun(counts.streams, "stream")} in {countNoun(counts.workspaces, "workspace")};{" "}
          {String(counts.behind)} behind, {String(counts.failing)} with failed runs.
        </Text>
      </Notice>
      <Facts>
        <Fact name="Read at">{formatInstant(status.asOf)}</Fact>
        <Fact name="Server version">{status.version === "" ? "—" : status.version}</Fact>
        <Fact name="Database">
          <Pill tone={status.databaseReachable ? "good" : "bad"}>{schemaWords(status)}</Pill>
        </Fact>
        <Fact name="Index">
          <Pill tone={index.tone}>{index.label}</Pill> {status.indexModel ?? ""}
        </Fact>
      </Facts>
      {status.indexNote === "" ? null : <Text tone="muted">{status.indexNote}</Text>}
      {counts.workspaces === 0 ? (
        <Text tone="muted">The token may read no workspace, or none exists yet.</Text>
      ) : null}
    </>
  );
}
