import { useCallback } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { formatInstant } from "../text/format-time";
import { ErrorNotice, Notice } from "../ui/notice";
import { Fact, Panel, Screen } from "../ui/panel";
import { Pill } from "../ui/pill";
import { useLoad } from "../ui/use-load";
import { indexVerdict, totals } from "./freshness";
import { readServiceStatus } from "./status";
import type { ServiceStatus } from "./status";
import { WorkspacePanel } from "./workspace-panel";

export interface StatusScreenProps {
  readonly connection: Connection;
}

export function StatusScreen({ connection }: StatusScreenProps): ReactElement {
  return (
    <Screen
      title="Status"
      lead="Whether the knowledge base is current: each source’s streams, their lag and their last run."
    >
      {connection.hasToken ? (
        <Status connection={connection} />
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <p>Store a token with the read scope on the Connection screen.</p>
        </Notice>
      )}
    </Screen>
  );
}

function Status({ connection }: StatusScreenProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readServiceStatus(backend, connection.id), [backend, connection.id]),
  );
  return (
    <>
      <Panel
        title="The base"
        actions={
          <button type="button" className="button small" disabled={loaded.busy} onClick={loaded.reload}>
            {loaded.busy ? "Refreshing…" : "Refresh"}
          </button>
        }
      >
        {loaded.state.phase === "loading" ? <p className="muted">Asking the server…</p> : null}
        {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
        {loaded.state.phase === "ready" ? <Summary status={loaded.state.data} /> : null}
      </Panel>
      {loaded.state.phase === "ready"
        ? loaded.state.data.workspaces.map((workspace, position) => (
            <WorkspacePanel key={`${String(position)}:${workspace.name}`} workspace={workspace} />
          ))
        : null}
    </>
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
        <p>
          {countNoun(counts.streams, "stream")} in {countNoun(counts.workspaces, "workspace")};{" "}
          {String(counts.behind)} behind, {String(counts.failing)} with failed runs.
        </p>
      </Notice>
      <dl className="facts">
        <Fact name="Read at">{formatInstant(status.asOf)}</Fact>
        <Fact name="Server version">{status.version === "" ? "—" : status.version}</Fact>
        <Fact name="Database">
          <Pill tone={status.databaseReachable ? "good" : "bad"}>{schemaWords(status)}</Pill>
        </Fact>
        <Fact name="Index">
          <Pill tone={index.tone}>{index.label}</Pill> {status.indexModel ?? ""}
        </Fact>
      </dl>
      {status.indexNote === "" ? null : <p className="muted">{status.indexNote}</p>}
      {counts.workspaces === 0 ? (
        <p className="muted">The token may read no workspace, or none exists yet.</p>
      ) : null}
    </>
  );
}
