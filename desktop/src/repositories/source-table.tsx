import { useCallback } from "react";
import type { ReactElement } from "react";

import {
  Button,
  ConfirmButton,
  DataTable,
  ErrorNotice,
  Mono,
  NoWrap,
  Panel,
  Pill,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { formatDuration } from "../text/format-time";
import type { GitSource, RefState } from "./repositories";
import { deleteSource, listGitSources, readRefStates, syncSource } from "./repositories-api";

export interface SourceTableProps {
  readonly connection: Connection;
  readonly workspace: string;
}

interface SourceState {
  readonly source: GitSource;
  readonly refs: readonly RefState[];
}

async function readSources(
  backend: Backend,
  id: string,
  workspace: string,
): Promise<readonly SourceState[]> {
  const sources = await listGitSources(backend, id, workspace);
  return Promise.all(
    sources.map(async (source) => ({
      source,
      refs: await readRefStates(backend, id, workspace, source.name),
    })),
  );
}

const COLUMNS = ["Name", "Location", "Sync", "Branches"];

/** The git sources of a workspace, each branch with how far it synced. */
export function SourceTable({ connection, workspace }: SourceTableProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readSources(backend, connection.id, workspace), [backend, connection.id, workspace]),
  );
  const { reload } = loaded;
  const sync = useCallback(
    async (name: string): Promise<void> => {
      await syncSource(backend, connection.id, workspace, name);
      reload();
    },
    [backend, connection.id, reload, workspace],
  );
  const remove = useCallback(
    async (name: string): Promise<void> => {
      await deleteSource(backend, connection.id, workspace, name);
      reload();
    },
    [backend, connection.id, reload, workspace],
  );
  const syncing = useAction(sync);
  const removing = useAction(remove);
  const busy = syncing.pending || removing.pending;
  return (
    <Panel
      title={`Repositories of ${workspace}`}
      actions={
        <Button disabled={loaded.busy} onClick={reload}>
          {loaded.busy ? "Refreshing…" : "Refresh"}
        </Button>
      }
    >
      {loaded.state.phase === "loading" ? <Text tone="muted">Asking the server…</Text> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      <ErrorNotice error={syncing.error ?? removing.error} />
      {loaded.state.phase !== "ready" ? null : loaded.state.data.length === 0 ? (
        <Text tone="muted">No repository yet. Add one above.</Text>
      ) : (
        <DataTable columns={COLUMNS} withActions>
          {loaded.state.data.map((item) => (
            <SourceRow
              key={item.source.name}
              item={item}
              disabled={busy}
              onSync={() => syncing.run(item.source.name)}
              onDelete={() => removing.run(item.source.name)}
            />
          ))}
        </DataTable>
      )}
    </Panel>
  );
}

interface SourceRowProps {
  readonly item: SourceState;
  readonly disabled: boolean;
  readonly onSync: () => Promise<void>;
  readonly onDelete: () => Promise<void>;
}

function SourceRow({ item, disabled, onSync, onDelete }: SourceRowProps): ReactElement {
  const { source, refs } = item;
  return (
    <tr>
      <th scope="row">{source.name}</th>
      <td>
        <Mono>{source.location}</Mono>
      </td>
      <td>
        <NoWrap>
          {source.scheduleSeconds === null ? "on demand" : `every ${formatDuration(source.scheduleSeconds)}`}
        </NoWrap>
      </td>
      <td>
        {refs.length === 0 ? (
          <Text tone="muted" small>
            {source.refs.join(", ")}
          </Text>
        ) : (
          refs.map((ref) => (
            <Text key={ref.ref} small>
              <Mono>{ref.ref.replace(/^refs\/heads\//u, "")}</Mono> <Pill tone={ref.tone}>{ref.label}</Pill>
              {ref.detail === null ? null : (
                <Text tone="failure" small inline>
                  {" "}
                  {ref.detail}
                </Text>
              )}
            </Text>
          ))
        )}
      </td>
      <td>
        <NoWrap>
          <Button disabled={disabled} onClick={onSync}>
            Sync now
          </Button>{" "}
          <ConfirmButton
            label="Delete"
            question={`Delete the source “${source.name}”? Its records, chunks and mirror are deleted; the repository itself is not touched.`}
            confirmLabel="Delete the source"
            disabled={disabled}
            onConfirm={onDelete}
          />
        </NoWrap>
      </td>
    </tr>
  );
}
