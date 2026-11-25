import { useCallback } from "react";
import type { ReactElement } from "react";

import {
  Button,
  DataTable,
  EmptyState,
  ErrorNotice,
  NoWrap,
  Notice,
  Panel,
  Pill,
  Screen,
  Section,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { formatDuration } from "../text/format-time";
import { readOverview } from "./data-api";
import type { SourceRow, WorkspaceOverview } from "./overview";
import { SensitiveRecords } from "./sensitive-records";
import { ToolConsole } from "./tool-console";

export interface DataScreenProps {
  readonly connection: Connection;
}

/**
 * What the base holds, and the read tools to try by hand. The one write here is the mark of a
 * record as sensitive, which an admin token may set or clear.
 */
export function DataScreen({ connection }: DataScreenProps): ReactElement {
  return (
    <Screen
      title="Data"
      lead="What the base holds, and what its read tools answer, without an AI agent. An admin token can also mark records sensitive."
    >
      {connection.hasToken ? (
        <>
          <Overview connection={connection} />
          <ToolConsole connection={connection} />
        </>
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <Text>Store a token with the read scope on the Connection screen.</Text>
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
        <Button disabled={loaded.busy} onClick={loaded.reload}>
          {loaded.busy ? "Refreshing…" : "Refresh"}
        </Button>
      }
    >
      {loaded.state.phase === "loading" ? <Text tone="muted">Asking the server…</Text> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {loaded.state.phase === "ready" ? (
        <Workspaces connection={connection} workspaces={loaded.state.data} />
      ) : null}
    </Panel>
  );
}

interface WorkspacesProps {
  readonly connection: Connection;
  readonly workspaces: readonly WorkspaceOverview[];
}

function Workspaces({ connection, workspaces }: WorkspacesProps): ReactElement {
  if (workspaces.length === 0) {
    return <EmptyState title="The token may read no workspace, or none exists yet" />;
  }
  return (
    <>
      {workspaces.map((workspace, position) => (
        <WorkspaceSources
          key={`${String(position)}:${workspace.name}`}
          connection={connection}
          workspace={workspace}
        />
      ))}
      <Text tone="muted" small>
        Chunks are counted per workspace: no route of the server counts them per source.
      </Text>
    </>
  );
}

interface WorkspaceSourcesProps {
  readonly connection: Connection;
  readonly workspace: WorkspaceOverview;
}

const SOURCE_COLUMNS = ["Source", "Kind", "Streams", "Records", "Not indexed", "Syncs every", "Freshness"];

function WorkspaceSources({ connection, workspace }: WorkspaceSourcesProps): ReactElement {
  const counts = (
    <>
      <Text tone="muted" inline>
        {countNoun(workspace.records, "record")}, {countNoun(workspace.chunks, "chunk")}
        {workspace.notIndexed > 0 ? `, ${String(workspace.notIndexed)} not indexed` : ""}
      </Text>
      <Pill tone={workspace.index.tone}>{workspace.index.label}</Pill>
    </>
  );
  return (
    <Section title={workspace.name} aside={counts}>
      {workspace.sources.length === 0 ? (
        <Text tone="muted">This workspace has no source yet.</Text>
      ) : (
        <DataTable columns={SOURCE_COLUMNS}>
          {workspace.sources.map((source, position) => (
            <SourceLine key={`${String(position)}:${source.name}`} source={source} />
          ))}
        </DataTable>
      )}
      <SensitiveRecords connection={connection} workspace={workspace.name} />
    </Section>
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
      <td>
        <NoWrap>
          {source.scheduleSeconds === null ? "on demand" : formatDuration(source.scheduleSeconds)}
        </NoWrap>
      </td>
      <td>
        <Pill tone={source.freshness.tone}>{source.freshness.label}</Pill>
      </td>
    </tr>
  );
}
