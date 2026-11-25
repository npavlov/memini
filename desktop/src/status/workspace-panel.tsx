import type { ReactElement } from "react";

import { DataTable, Mono, NoWrap, Panel, Pill, Text } from "../components";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { formatAgo, formatDuration } from "../text/format-time";
import { runTone, streamVerdict, workspaceIndexVerdict } from "./freshness";
import { IndexRebuilds } from "./index-rebuilds";
import type { StreamRun, StreamStatus, WorkspaceStatus } from "./status";

export interface WorkspacePanelProps {
  readonly workspace: WorkspaceStatus;
}

/** One workspace: its index, and a row per stream. Every value is the server's text, shown as text. */
export function WorkspacePanel({ workspace }: WorkspacePanelProps): ReactElement {
  const { index } = workspace;
  const verdict = workspaceIndexVerdict(index);
  return (
    <Panel title={`Workspace: ${workspace.name}`} actions={<Pill tone={verdict.tone}>{verdict.label}</Pill>}>
      <Text tone="muted">
        {countNoun(index.records, "record")}, {countNoun(index.chunks, "chunk")}
        {index.recordsNotIndexed > 0 ? `, ${String(index.recordsNotIndexed)} not indexed` : ""}
        {index.builtWith === null ? "" : `; index built with ${index.builtWith}`}
      </Text>
      {index.rebuild !== null ? (
        <IndexRebuilds rebuilds={[{ workspace: workspace.name, rebuild: index.rebuild }]} />
      ) : index.reindex === null ? null : (
        <Text>
          To bring the index up to date, the server suggests running, where its CLI is:{" "}
          <Mono>{index.reindex}</Mono>
        </Text>
      )}
      {workspace.streams.length === 0 ? (
        <Text tone="muted">No source has a stream yet.</Text>
      ) : (
        <StreamsTable streams={workspace.streams} />
      )}
    </Panel>
  );
}

interface StreamsTableProps {
  readonly streams: readonly StreamStatus[];
}

const STREAM_COLUMNS = ["Source", "Stream", "Freshness", "Lag", "Every", "Last run", "Records"];

function StreamsTable({ streams }: StreamsTableProps): ReactElement {
  return (
    <DataTable columns={STREAM_COLUMNS}>
      {streams.map((stream, position) => (
        <StreamRow key={`${String(position)}:${stream.source}:${stream.key}`} stream={stream} />
      ))}
    </DataTable>
  );
}

interface StreamRowProps {
  readonly stream: StreamStatus;
}

function StreamRow({ stream }: StreamRowProps): ReactElement {
  const verdict = streamVerdict(stream);
  return (
    <tr>
      <th scope="row">
        {stream.source}{" "}
        <Text tone="muted" small inline>
          {stream.sourceKind}
        </Text>
      </th>
      <td>
        <Mono>{stream.key}</Mono>
      </td>
      <td>
        <Pill tone={verdict.tone}>{verdict.label}</Pill>
        {stream.historyLoaded === false ? (
          <Text tone="muted" small inline>
            {" "}
            history loading
          </Text>
        ) : null}
      </td>
      <td>
        <NoWrap>{formatDuration(stream.lagSeconds)}</NoWrap>
      </td>
      <td>
        <NoWrap>
          {stream.scheduleSeconds === null ? "on demand" : formatDuration(stream.scheduleSeconds)}
        </NoWrap>
      </td>
      <td>
        <LastRun run={stream.lastRun} />
        <Failures stream={stream} />
      </td>
      <td>
        {String(stream.records)}
        {stream.recordsNotIndexed > 0 ? ` (${String(stream.recordsNotIndexed)} not indexed)` : ""}
      </td>
    </tr>
  );
}

interface LastRunProps {
  readonly run: StreamRun | null;
}

function LastRun({ run }: LastRunProps): ReactElement {
  const backend = useBackend();
  if (run === null) {
    return (
      <Text tone="muted" inline>
        never ran
      </Text>
    );
  }
  return (
    <>
      <Pill tone={runTone(run.status)}>{run.status === "" ? "unknown" : run.status}</Pill>{" "}
      <Text tone="muted" small inline>
        {formatAgo(run.finishedAt ?? run.startedAt, backend.now())}
      </Text>
    </>
  );
}

function Failures({ stream }: StreamRowProps): ReactElement | null {
  if (stream.failures === 0) {
    return null;
  }
  const error = stream.lastFailure?.error ?? null;
  return (
    <Text tone="failure" small>
      {String(stream.failures)} failed in a row{error === null ? "" : `: ${error}`}
    </Text>
  );
}
