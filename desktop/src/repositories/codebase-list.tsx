import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Card,
  ConfirmButton,
  CopyButton,
  EmptyState,
  ErrorNotice,
  Panel,
  Pill,
  ProgressBar,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { formatAgo, formatDuration } from "../text/format-time";
import { agentTask, describeProgress } from "./codebases";
import type { SyncProgress } from "./codebases";
import type { GitSource, RefState } from "./repositories";
import {
  deleteSource,
  listGitSources,
  readRefStates,
  readSyncProgress,
  syncSource,
  updateSource,
} from "./repositories-api";
import { ScopeEditor } from "./scope-editor";

export interface CodebaseListProps {
  readonly connection: Connection;
  readonly workspace: string;
}

interface Codebase {
  readonly source: GitSource;
  readonly refs: readonly RefState[];
  /** How far the running sync of each ref is, by ref; missing before the sync says. */
  readonly progress: ReadonlyMap<string, SyncProgress>;
}

/** How often the list is read again while a sync runs. */
const POLL_MS = 4000;
/** The schedule a code base gets back when it is resumed. */
const RESUME_SECONDS = 3600;

async function readCodebases(backend: Backend, id: string, workspace: string): Promise<readonly Codebase[]> {
  const sources = await listGitSources(backend, id, workspace);
  return Promise.all(
    sources.map(async (source) => {
      const refs = await readRefStates(backend, id, workspace, source.name);
      const progress = new Map<string, SyncProgress>();
      await Promise.all(
        refs.map(async (ref) => {
          if (ref.running && ref.runId !== null) {
            const found = await readSyncProgress(backend, id, ref.runId);
            if (found !== null) {
              progress.set(ref.ref, found);
            }
          }
        }),
      );
      return { source, refs, progress };
    }),
  );
}

/** The code bases of a workspace, each with how far it synced and what it indexes. */
export function CodebaseList({ connection, workspace }: CodebaseListProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readCodebases(backend, connection.id, workspace), [backend, connection.id, workspace]),
  );
  const { reload } = loaded;
  const codebases = loaded.state.phase === "ready" ? loaded.state.data : null;
  const syncing = codebases?.some((codebase) => codebase.refs.some((ref) => ref.running)) ?? false;
  useEffect(() => {
    if (!syncing) {
      return undefined;
    }
    const timer = window.setInterval(reload, POLL_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [syncing, reload]);
  const task = agentTask(
    workspace,
    (codebases ?? []).map((codebase) => ({ name: codebase.source.name, location: codebase.source.location })),
  );
  return (
    <Panel
      title={`Code bases of ${workspace}`}
      actions={
        <>
          <CopyButton text={task} label="Copy task for an agent" />
          <Button disabled={loaded.busy} onClick={reload}>
            {loaded.busy ? "Refreshing…" : "Refresh"}
          </Button>
        </>
      }
    >
      <Text tone="muted">
        An agent connected to Memini maps these code bases to the business: “Copy task for an agent”
        gives it what to read and how to write the project map, which you then check on the Project
        map screen.
      </Text>
      {loaded.state.phase === "loading" ? <Text tone="muted">Asking the server…</Text> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {codebases === null ? null : codebases.length === 0 ? (
        <EmptyState title="No code base yet">
          <Text>Add a folder of this computer or a repository at an address.</Text>
        </EmptyState>
      ) : (
        codebases.map((codebase) => (
          <CodebaseCard
            key={codebase.source.name}
            connection={connection}
            workspace={workspace}
            codebase={codebase}
            onChanged={reload}
          />
        ))
      )}
    </Panel>
  );
}

interface CodebaseCardProps {
  readonly connection: Connection;
  readonly workspace: string;
  readonly codebase: Codebase;
  readonly onChanged: () => void;
}

function scheduleWords(seconds: number | null): string {
  return seconds === null ? "Paused: syncs only when asked" : `Syncs every ${formatDuration(seconds)}`;
}

function shortRef(ref: string): string {
  return ref.replace(/^refs\/(heads|tags)\//u, "");
}

function lastSynced(refs: readonly RefState[], now: number): string | null {
  const finished = refs.map((ref) => ref.finishedAt).filter((at): at is string => at !== null);
  const newest = finished.sort().at(-1) ?? null;
  return newest === null ? null : `last synced ${formatAgo(newest, now)}`;
}

function CodebaseCard({ connection, workspace, codebase, onChanged }: CodebaseCardProps): ReactElement {
  const backend = useBackend();
  const { source, refs, progress } = codebase;
  const [editing, setEditing] = useState(false);
  const act = useCallback(
    async (what: "sync" | "pause" | "resume" | "delete"): Promise<void> => {
      if (what === "sync") {
        await syncSource(backend, connection.id, workspace, source.name);
      } else if (what === "delete") {
        await deleteSource(backend, connection.id, workspace, source.name);
      } else {
        const schedule = what === "pause" ? "off" : RESUME_SECONDS;
        await updateSource(backend, connection.id, workspace, source.name, { schedule });
      }
      onChanged();
    },
    [backend, connection.id, onChanged, source.name, workspace],
  );
  const acting = useAction(act);
  const now = Date.now();
  const synced = lastSynced(refs, now);
  const paused = source.scheduleSeconds === null;
  return (
    <Card title={source.name} subtitle={source.location}>
      <Actions>
        {refs.map((ref) => (
          <Pill key={ref.ref} tone={ref.tone}>
            {shortRef(ref.ref)}: {ref.label}
          </Pill>
        ))}
        <Text tone="muted" small inline>
          {scheduleWords(source.scheduleSeconds)}
          {synced === null ? "" : ` · ${synced}`}
        </Text>
      </Actions>
      {refs
        .filter((ref) => ref.running)
        .map((ref) => {
          const found = progress.get(ref.ref) ?? null;
          return (
            <ProgressBar
              key={ref.ref}
              label={`Files of ${source.name}, ${shortRef(ref.ref)}`}
              value={found?.processed ?? 0}
              max={found?.total ?? 0}
              detail={found === null ? "Fetching and listing the repository…" : describeProgress(found, now)}
            />
          );
        })}
      {refs
        .filter((ref) => ref.detail !== null)
        .map((ref) => (
          <Text key={ref.ref} tone="failure" small>
            {shortRef(ref.ref)}: {ref.detail}
          </Text>
        ))}
      <ErrorNotice error={acting.error} />
      <Actions>
        <Button disabled={acting.pending} onClick={() => acting.run("sync")}>
          Sync now
        </Button>
        <Button disabled={acting.pending} onClick={() => acting.run(paused ? "resume" : "pause")}>
          {paused ? "Resume" : "Pause"}
        </Button>
        <Button
          onClick={() => {
            setEditing(!editing);
          }}
        >
          {editing ? "Close" : "What it indexes…"}
        </Button>
        <ConfirmButton
          label="Delete"
          question={`Delete ${source.name}? Its records, chunks and mirror are removed from the server; the repository itself is not touched.`}
          confirmLabel="Delete the code base"
          disabled={acting.pending}
          onConfirm={() => acting.run("delete")}
        />
      </Actions>
      {editing ? (
        <ScopeEditor
          connection={connection}
          workspace={workspace}
          source={source}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
        />
      ) : null}
    </Card>
  );
}
