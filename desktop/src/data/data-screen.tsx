import { useCallback } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { formatDuration } from "../text/format-time";
import { DataTable } from "../ui/data-table";
import { ErrorNotice, Notice } from "../ui/notice";
import { Panel, Screen } from "../ui/panel";
import { Pill } from "../ui/pill";
import { useLoad } from "../ui/use-load";
import { readOverview } from "./data-api";
import type { SourceRow, WorkspaceOverview } from "./overview";
import { ToolConsole } from "./tool-console";

export interface DataScreenProps {
  readonly connection: Connection;
}

/** What the base holds, and the read tools to try by hand. Nothing on this screen can write. */
export function DataScreen({ connection }: DataScreenProps): ReactElement {
  return (
    <Screen
      title="Data"
      lead="What the base holds, and what its read tools answer, without an AI agent. Read-only."
    >
      {connection.hasToken ? (
        <>
          <Overview connection={connection} />
          <ToolConsole connection={connection} />
        </>
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <p>Store a token with the read scope on the Connection screen.</p>
        </Notice>
      )}
    </Screen>
  );
}

function Overview({ connection }: DataScreenProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readOverview(backend, connection.id), [backend, connection.id]),
  );
  return (
    <Panel
      title="What the base holds"
      actions={
        <button type="button" className="button" disabled={loaded.busy} onClick={loaded.reload}>
          {loaded.busy ? "Refreshing…" : "Refresh"}
        </button>
      }
    >
      {loaded.state.phase === "loading" ? <p className="muted">Asking the server…</p> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {loaded.state.phase === "ready" ? <Workspaces workspaces={loaded.state.data} /> : null}
    </Panel>
  );
}

interface WorkspacesProps {
  readonly workspaces: readonly WorkspaceOverview[];
}

function Workspaces({ workspaces }: WorkspacesProps): ReactElement {
  if (workspaces.length === 0) {
    return <p className="muted">The token may read no workspace, or none exists yet.</p>;
  }
  return (
    <>
      {workspaces.map((workspace, position) => (
        <WorkspaceSources key={`${String(position)}:${workspace.name}`} workspace={workspace} />
      ))}
      <p className="muted small">
        Chunks are counted per workspace: no route of the server counts them per source.
      </p>
    </>
  );
}

interface WorkspaceSourcesProps {
  readonly workspace: WorkspaceOverview;
}

const SOURCE_COLUMNS = ["Source", "Kind", "Streams", "Records", "Not indexed", "Syncs every", "Freshness"];

function WorkspaceSources({ workspace }: WorkspaceSourcesProps): ReactElement {
  return (
    <section className="subsection">
      <header className="subsection-header">
        <h3>{workspace.name}</h3>
        <span className="muted">
          {countNoun(workspace.records, "record")}, {countNoun(workspace.chunks, "chunk")}
          {workspace.notIndexed > 0 ? `, ${String(workspace.notIndexed)} not indexed` : ""}
        </span>
        <Pill tone={workspace.index.tone}>{workspace.index.label}</Pill>
      </header>
      {workspace.sources.length === 0 ? (
        <p className="muted">This workspace has no source yet.</p>
      ) : (
        <DataTable columns={SOURCE_COLUMNS}>
          {workspace.sources.map((source, position) => (
            <SourceLine key={`${String(position)}:${source.name}`} source={source} />
          ))}
        </DataTable>
      )}
    </section>
  );
}

interface SourceLineProps {
  readonly source: SourceRow;
}

function SourceLine({ source }: SourceLineProps): ReactElement {
  return (
    <tr>
      <th scope="row">{source.name}</th>
      <td>{source.kind === "" ? "—" : source.kind}</td>
      <td>{String(source.streams)}</td>
      <td>{String(source.records)}</td>
      <td>{String(source.notIndexed)}</td>
      <td className="nowrap">
        {source.scheduleSeconds === null ? "on demand" : formatDuration(source.scheduleSeconds)}
      </td>
      <td>
        <Pill tone={source.freshness.tone}>{source.freshness.label}</Pill>
      </td>
    </tr>
  );
}
