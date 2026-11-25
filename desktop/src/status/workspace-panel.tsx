import type { ReactElement } from "react";

import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { formatAgo, formatDuration } from "../text/format-time";
import { DataTable } from "../ui/data-table";
import { Panel } from "../ui/panel";
import { Pill } from "../ui/pill";
import { runTone, streamVerdict, workspaceIndexVerdict } from "./freshness";
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
      <p className="muted">
        {countNoun(index.records, "record")}, {countNoun(index.chunks, "chunk")}
        {index.recordsNotIndexed > 0 ? `, ${String(index.recordsNotIndexed)} not indexed` : ""}
        {index.builtWith === null ? "" : `; index built with ${index.builtWith}`}
      </p>
      {index.reindex === null ? null : (
        <p>
          To bring the index up to date, the server suggests running, where its CLI is:{" "}
          <code>{index.reindex}</code>
        </p>
      )}
      {workspace.streams.length === 0 ? (
        <p className="muted">No source has a stream yet.</p>
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
        {stream.source} <span className="muted small">{stream.sourceKind}</span>
      </th>
      <td>{stream.key}</td>
      <td>
        <Pill tone={verdict.tone}>{verdict.label}</Pill>
        {stream.historyLoaded === false ? <span className="muted small"> history loading</span> : null}
      </td>
      <td className="nowrap">{formatDuration(stream.lagSeconds)}</td>
      <td className="nowrap">
        {stream.scheduleSeconds === null ? "on demand" : formatDuration(stream.scheduleSeconds)}
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
    return <span className="muted">never ran</span>;
  }
  return (
    <>
      <Pill tone={runTone(run.status)}>{run.status === "" ? "unknown" : run.status}</Pill>{" "}
      <span className="muted small">{formatAgo(run.finishedAt ?? run.startedAt, backend.now())}</span>
    </>
  );
}

function Failures({ stream }: StreamRowProps): ReactElement | null {
  if (stream.failures === 0) {
    return null;
  }
  const error = stream.lastFailure?.error ?? null;
  return (
    <p className="failure small">
      {String(stream.failures)} failed in a row{error === null ? "" : `: ${error}`}
    </p>
  );
}
