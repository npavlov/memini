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
  Panel,
  Pill,
  ProgressBar,
  Section,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { formatAgo, formatClock, formatDuration } from "../text/format-time";
import {
  MAPPING_STEPS,
  SYNC_EXPLAINED,
  agentTask,
  describeCheck,
  describeCoverage,
  describeHeld,
  describeProgress,
  progressLabel,
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

/** How often the list is read again while a sync runs. */
const POLL_MS = 4000;
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
  return (
    <>
      <Panel
        title={`Code bases of ${workspace}`}
        actions={
          <Button disabled={loaded.busy} onClick={reload}>
            {loaded.busy ? "Refreshing…" : "Refresh"}
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
  readonly onChanged: () => void;
}

function scheduleWords(seconds: number | null): string {
  return seconds === null ? "Paused: syncs only when asked" : `Syncs every ${formatDuration(seconds)}`;
}

function shortRef(ref: string): string {
  return ref.replace(/^refs\/(heads|tags)\//u, "");
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

/** "07:37 — no changes"; null while it runs or before any run. */
function checkWords(ref: RefState, check: RunCheck | undefined, now: number): string | null {
  if (ref.running || ref.finishedAt === null) {
    return null;
  }
  const at = formatClock(ref.finishedAt, now);
  if (ref.detail !== null) {
    return `${at} — ${ref.label}: ${ref.detail}`;
  }
  return check === undefined ? `${at} — ${ref.label}` : `${at} — ${describeCheck(check)}`;
}

function CodebaseCard(props: CodebaseCardProps): ReactElement {
  const { connection, workspace, codebase, folder, tokenNames, onChanged } = props;
  const backend = useBackend();
  const { source, refs, checks, coverage } = codebase;
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
  const paused = source.scheduleSeconds === null;
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
      <Mapping coverage={coverage} tokenNames={tokenNames} now={now} />
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
          question={`Delete ${source.name}? Its records, chunks and mirror are removed from the server; the repository itself is not touched.${keep}`}
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
  const checked = checkWords(state, check, now);
  return (
    <>
      <Fact name={`Index of ${name}`}>{heldWords(state, now)}</Fact>
      {checked === null ? null : <Fact name="Last check">{checked}</Fact>}
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
