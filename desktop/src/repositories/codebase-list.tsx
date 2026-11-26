import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Card,
  CodeBlock,
  ConfirmButton,
  EmptyState,
  ErrorNotice,
  Fact,
  Facts,
  Mono,
  Notice,
  Panel,
  Pill,
  ProgressBar,
  Section,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import type { Action } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { formatAgo, formatClock, formatDuration } from "../text/format-time";
import {
  LOCAL_FOLDER_SYNC,
  MAPPING_STEPS,
  SYNC_EXPLAINED,
  agentTask,
  describeCoverage,
  describeHeld,
  describeOutcome,
  describeProgress,
  endedSyncs,
  progressLabel,
  shortRef,
  syncPhase,
} from "./codebases";
import type { Coverage, RunCheck } from "./codebases";
import { folderOf } from "./repositories";
import type { GitSource, LocalRepo, RefState } from "./repositories";
import {
  deleteSource,
  listGitSources,
  readCoverage,
  readRefStates,
  readRunCheck,
  syncSource,
  updateSource,
} from "./repositories-api";
import { ScopeEditor } from "./scope-editor";

export interface CodebaseListProps {
  readonly connection: Connection;
  readonly workspace: string;
  /** The folders given to the local server; null for a connection to another server. */
  readonly folders: readonly LocalRepo[] | null;
  /** The names of the tokens by id, to say who wrote the project map; empty when unknown. */
  readonly tokenNames: ReadonlyMap<string, string>;
}

interface Codebase {
  readonly source: GitSource;
  readonly refs: readonly RefState[];
  /** What the newest run of each ref did or does, by ref; missing when it cannot be read. */
  readonly checks: ReadonlyMap<string, RunCheck>;
  /** How much the project map names; null when the server cannot say. */
  readonly coverage: Coverage | null;
}

/** How often the list is read again while a sync waits in the queue or runs. */
const POLL_MS = 2000;
/** The schedule a code base gets back when it is resumed. */
const RESUME_SECONDS = 3600;

async function orNull<T>(read: Promise<T>): Promise<T | null> {
  try {
    return await read;
  } catch {
    return null;
  }
}

async function readCodebase(backend: Backend, id: string, workspace: string, source: GitSource): Promise<Codebase> {
  const [refs, coverage] = await Promise.all([
    readRefStates(backend, id, workspace, source.name),
    orNull(readCoverage(backend, id, workspace, source.name)),
  ]);
  const checks = new Map<string, RunCheck>();
  await Promise.all(
    refs.map(async (ref) => {
      if (ref.runId !== null) {
        const found = await orNull(readRunCheck(backend, id, ref.runId));
        if (found !== null) {
          checks.set(ref.ref, found);
        }
      }
    }),
  );
  return { source, refs, checks, coverage };
}

async function readCodebases(backend: Backend, id: string, workspace: string): Promise<readonly Codebase[]> {
  const sources = await listGitSources(backend, id, workspace);
  return Promise.all(sources.map((source) => readCodebase(backend, id, workspace, source)));
}

/** The code bases of a workspace, each with its folder, its index, its last check and its mapping. */
export function CodebaseList({ connection, workspace, folders, tokenNames }: CodebaseListProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readCodebases(backend, connection.id, workspace), [backend, connection.id, workspace]),
  );
  const { reload } = loaded;
  const codebases = loaded.state.phase === "ready" ? loaded.state.data : null;
  // A queued sync is followed as closely as a running one: it starts and may end between two reads.
  const syncing = codebases?.some((codebase) => syncPhase(codebase.refs) !== null) ?? false;
  useEffect(() => {
    if (!syncing) {
      return undefined;
    }
    const timer = window.setInterval(reload, POLL_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [syncing, reload]);
  return (
    <>
      <Panel
        title={`Code bases of ${workspace}`}
        actions={
          <Button busy={loaded.busy && !syncing ? "Refreshing…" : null} onClick={reload}>
            Refresh
          </Button>
        }
      >
        <Text tone="muted" small>
          {SYNC_EXPLAINED}
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
              folder={folders === null ? null : folderOf(folders, codebase.source.location)}
              tokenNames={tokenNames}
              onChanged={reload}
            />
          ))
        )}
      </Panel>
      {codebases === null || codebases.length === 0 ? null : (
        <AgentTask workspace={workspace} codebases={codebases} />
      )}
    </>
  );
}

interface AgentTaskProps {
  readonly workspace: string;
  readonly codebases: readonly Codebase[];
}

/** The task that maps the code bases to the business, shown before it is copied. */
function AgentTask({ workspace, codebases }: AgentTaskProps): ReactElement {
  const [open, setOpen] = useState(false);
  const task = agentTask(
    workspace,
    codebases.map((codebase) => ({ name: codebase.source.name, location: codebase.source.location })),
  );
  return (
    <Panel
      title="Map the code bases to the business"
      actions={
        <Button
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Hide the task" : "Show the task for an agent"}
        </Button>
      }
    >
      <Text>
        The project map says which projects, business domains and subdomains each part of the code
        serves; search and the agents use it to answer “where is X”. An agent writes it for you:
      </Text>
      {MAPPING_STEPS.map((step, position) => (
        <Text key={step}>
          {String(position + 1)}. {step}
        </Text>
      ))}
      {open ? <CodeBlock label="Task for an agent" text={task} wrap bounded /> : null}
    </Panel>
  );
}

interface CodebaseCardProps {
  readonly connection: Connection;
  readonly workspace: string;
  readonly codebase: Codebase;
  readonly folder: LocalRepo | null;
  readonly tokenNames: ReadonlyMap<string, string>;
  /** Reads the code bases again; settles once the card shows what was read. */
  readonly onChanged: () => Promise<void>;
}

function scheduleWords(seconds: number | null): string {
  return seconds === null ? "Paused: syncs only when asked" : `Syncs every ${formatDuration(seconds)}`;
}

/** "1,472 files · 9,310 chunks · commit 1a2b3c4 of 25 Nov 2026, 14:02". */
function heldWords(ref: RefState, now: number): string {
  const index = ref.index;
  if (index === null) {
    return "The server does not say.";
  }
  if (index.commit === null && index.files === 0) {
    return "Nothing indexed yet.";
  }
  const held = describeHeld(index.files, index.chunks);
  if (index.commit === null) {
    return `${held} · the last sync stopped part way; the next one lists the branch again`;
  }
  const made = index.committedAt === null ? "" : ` of ${formatClock(index.committedAt, now)}`;
  return `${held} · commit ${index.commit.slice(0, 7)}${made}`;
}

/** A Sync now the user asked for: the newest run of each ref then, and whether it was seen under way. */
interface AskedSync {
  readonly before: ReadonlyMap<string, string | null>;
  readonly seenUnderWay: boolean;
}

type CardAction = "sync" | "pause" | "resume" | "delete";

const WORKING: Readonly<Record<CardAction, string>> = {
  sync: "Asking…",
  pause: "Pausing…",
  resume: "Resuming…",
  delete: "Deleting…",
};

/**
 * Follows a Sync now from the click to its end: the request, the queue, the run. The card's data
 * is read again every two seconds while a sync waits or runs, so this only remembers what it
 * asked for.
 */
function useAskedSync(refs: readonly RefState[]): {
  readonly asked: AskedSync | null;
  readonly ask: () => void;
} {
  const [asked, setAsked] = useState<AskedSync | null>(null);
  const underWay = syncPhase(refs) !== null;
  useEffect(() => {
    if (underWay) {
      setAsked((current) => (current === null || current.seenUnderWay ? current : { ...current, seenUnderWay: true }));
    }
  }, [underWay]);
  const ask = useCallback((): void => {
    setAsked({ before: new Map(refs.map((ref) => [ref.ref, ref.runId])), seenUnderWay: false });
  }, [refs]);
  return { asked, ask };
}

function syncBusy(acting: Action<CardAction>, phase: "running" | "queued" | null): string | null {
  if (phase === "running") {
    return "Syncing…";
  }
  if (phase === "queued") {
    return "Queued…";
  }
  return acting.running?.argument === "sync" ? WORKING.sync : null;
}

function CodebaseCard(props: CodebaseCardProps): ReactElement {
  const { connection, workspace, codebase, folder, tokenNames, onChanged } = props;
  const backend = useBackend();
  const { source, refs, checks, coverage } = codebase;
  const [editing, setEditing] = useState(false);
  const { asked, ask } = useAskedSync(refs);
  const act = useCallback(
    async (what: CardAction): Promise<void> => {
      if (what === "sync") {
        ask();
        await syncSource(backend, connection.id, workspace, source.name);
      } else if (what === "delete") {
        await deleteSource(backend, connection.id, workspace, source.name);
      } else {
        const schedule = what === "pause" ? "off" : RESUME_SECONDS;
        await updateSource(backend, connection.id, workspace, source.name, { schedule });
      }
      // The action, and so its button, ends only once the card shows the stream's state read
      // after the request: a queued or running sync, or a run that finished since the click.
      await onChanged();
    },
    [ask, backend, connection.id, onChanged, source.name, workspace],
  );
  const acting = useAction(act);
  const now = Date.now();
  const paused = source.scheduleSeconds === null;
  const phase = syncPhase(refs);
  const working = (what: CardAction): string | null => (acting.running?.argument === what ? WORKING[what] : null);
  const keep = folder === null ? "" : " The folder stays given to the server: take it back on the Server screen.";
  return (
    <Card title={source.name} subtitle={folder === null ? source.location : ""}>
      <Actions>
        {refs.map((ref) => (
          <Pill key={ref.ref} tone={ref.tone}>
            {shortRef(ref.ref)}: {ref.label}
          </Pill>
        ))}
        <Text tone="muted" small inline>
          {scheduleWords(source.scheduleSeconds)}
        </Text>
      </Actions>
      <Facts>
        {folder === null ? null : <FolderFacts folder={folder} />}
        {refs.map((ref) => (
          <RefFacts key={ref.ref} name={shortRef(ref.ref)} state={ref} check={checks.get(ref.ref)} now={now} />
        ))}
      </Facts>
      {refs
        .filter((ref) => ref.running)
        .map((ref) => {
          const check = checks.get(ref.ref);
          const progress = check?.progress ?? null;
          return (
            <ProgressBar
              key={ref.ref}
              label={`${progressLabel(check?.mode ?? null)} (${shortRef(ref.ref)})`}
              value={progress?.processed ?? 0}
              max={progress?.total ?? 0}
              detail={progress === null ? "Fetching and comparing with the indexed commit…" : describeProgress(progress, now)}
            />
          );
        })}
      <SyncEnded asked={asked} refs={refs} checks={checks} now={now} />
      <Mapping coverage={coverage} tokenNames={tokenNames} now={now} />
      <ErrorNotice error={acting.error} />
      <Actions>
        <Button disabled={acting.pending} busy={syncBusy(acting, phase)} onClick={() => acting.run("sync")}>
          Sync now
        </Button>
        <Button
          disabled={acting.pending}
          busy={working(paused ? "resume" : "pause")}
          onClick={() => acting.run(paused ? "resume" : "pause")}
        >
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
          question={`Delete ${source.name}? Its records, chunks and mirror are removed from the server; the repository itself is not touched.${keep}`}
          confirmLabel="Delete the code base"
          working={WORKING.delete}
          disabled={acting.pending}
          onConfirm={() => acting.run("delete")}
        />
      </Actions>
      {folder === null ? null : (
        <Text tone="muted" small>
          {LOCAL_FOLDER_SYNC}
        </Text>
      )}
      {editing ? (
        <ScopeEditor
          connection={connection}
          workspace={workspace}
          source={source}
          onSaved={async () => {
            await onChanged();
            setEditing(false);
          }}
        />
      ) : null}
    </Card>
  );
}

interface SyncEndedProps {
  readonly asked: AskedSync | null;
  readonly refs: readonly RefState[];
  readonly checks: ReadonlyMap<string, RunCheck>;
  readonly now: number;
}

/** How the Sync now the user asked for ended, ref by ref, once no sync waits or runs. */
function SyncEnded({ asked, refs, checks, now }: SyncEndedProps): ReactElement | null {
  if (asked === null) {
    return null;
  }
  const lines = endedSyncs(refs, asked.before, asked.seenUnderWay).flatMap((ref) => {
    const line = describeOutcome(ref, checks.get(ref.ref), now);
    return line === null ? [] : [{ ref, line }];
  });
  if (lines.length === 0) {
    return null;
  }
  const failed = lines.some(({ ref }) => ref.tone === "bad");
  return (
    <Notice tone={failed ? "bad" : "good"} title={failed ? "The sync failed" : "Sync finished"}>
      {lines.map(({ ref, line }) => (
        <Text key={ref.ref}>{line}</Text>
      ))}
    </Notice>
  );
}

function FolderFacts({ folder }: { readonly folder: LocalRepo }): ReactElement {
  return (
    <>
      <Fact name="Folder on this computer">
        <Mono>{folder.path}</Mono>
      </Fact>
      <Fact name="The server sees it at">
        <Mono>{folder.location}</Mono>
      </Fact>
      <Fact name="Checked out">
        {folder.present ? (
          <Pill tone="good">{folder.branch === null ? "detached HEAD" : `on ${folder.branch}`}</Pill>
        ) : (
          <Pill tone="bad">folder is gone</Pill>
        )}
      </Fact>
    </>
  );
}

interface RefFactsProps {
  readonly name: string;
  readonly state: RefState;
  readonly check: RunCheck | undefined;
  readonly now: number;
}

function RefFacts({ name, state, check, now }: RefFactsProps): ReactElement {
  const checked = describeOutcome(state, check, now);
  return (
    <>
      <Fact name={`Index of ${name}`}>{heldWords(state, now)}</Fact>
      {checked === null ? null : <Fact name="Last sync">{checked}</Fact>}
    </>
  );
}

interface MappingProps {
  readonly coverage: Coverage | null;
  readonly tokenNames: ReadonlyMap<string, string>;
  readonly now: number;
}

/** Whether the project map names this code base, and what of it no domain names. */
function Mapping({ coverage, tokenNames, now }: MappingProps): ReactElement {
  if (coverage === null) {
    return (
      <Section title="Project map" aside={<Pill tone="idle">unknown</Pill>}>
        <Text tone="muted" small>
          The server does not say how much of the code base the project map names.
        </Text>
      </Section>
    );
  }
  const mapped = coverage.projects.length > 0;
  return (
    <Section title="Project map" aside={<Pill tone={mapped ? "good" : "idle"}>{mapped ? "mapped" : "not mapped"}</Pill>}>
      <Text small>{describeCoverage(coverage)}</Text>
      {coverage.projects.map((project) => {
        const who = project.updatedToken === null ? null : (tokenNames.get(project.updatedToken) ?? null);
        const domains = project.domains.length === 0 ? "no domain yet" : project.domains.join(", ");
        return (
          <Text key={project.project} tone="muted" small>
            {project.project}: {domains} · {String(project.files)} files · written {formatAgo(project.updatedAt, now)}
            {who === null ? "" : ` by the token “${who}”`}
          </Text>
        );
      })}
      {mapped && coverage.uncovered.length > 0 ? (
        <Text tone="muted" small>
          In no domain: {coverage.uncovered.map((folder) => `${folder.folder} (${String(folder.files)})`).join(", ")}
        </Text>
      ) : null}
    </Section>
  );
}
