// What the base holds, per workspace and source, from the routes that exist: the status route
// (streams with their records and freshness, the workspace's index) and the list of a
// workspace's sources. Chunks are counted per workspace only; no route counts them per source.

import { readArray, readNumber, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";
import { streamVerdict, workspaceIndexVerdict } from "../status/freshness";
import type { Verdict } from "../status/freshness";
import type { StreamStatus, WorkspaceStatus } from "../status/status";
import type { Tone } from "../ui/tone";

/** A source as the list of a workspace's sources names it. */
export interface ListedSource {
  readonly name: string;
  readonly kind: string;
  readonly scheduleSeconds: number | null;
}

export interface SourceRow {
  readonly name: string;
  readonly kind: string;
  readonly scheduleSeconds: number | null;
  readonly streams: number;
  readonly records: number;
  readonly notIndexed: number;
  readonly freshness: Verdict;
}

export interface WorkspaceOverview {
  readonly name: string;
  readonly records: number;
  readonly chunks: number;
  readonly notIndexed: number;
  readonly index: Verdict;
  readonly sources: readonly SourceRow[];
}

const SEVERITY: Readonly<Record<Tone, number>> = { bad: 3, warn: 2, good: 1, info: 0, idle: 0 };
const NO_STREAM: Verdict = { tone: "idle", label: "no stream yet" };

/** The sources of `GET /v1/workspaces/{workspace}/sources`. */
export function parseListedSources(value: unknown): readonly ListedSource[] {
  const list = readObject(value, "the list of sources");
  return readArray(list["sources"]).map((item) => {
    const source = readObject(item, "a source");
    return {
      name: displayText(readString(source["name"]), 80),
      kind: displayText(readString(source["kind"]), 20),
      scheduleSeconds: readNumber(source["schedule_seconds"]),
    };
  });
}

/** The freshness of a source: that of its stream that is worst off. */
function worstOf(streams: readonly StreamStatus[]): Verdict {
  return streams
    .map(streamVerdict)
    .reduce<Verdict>(
      (worst, verdict) => (SEVERITY[verdict.tone] > SEVERITY[worst.tone] ? verdict : worst),
      streams.length === 0 ? NO_STREAM : { tone: "idle", label: "on demand" },
    );
}

function sum(numbers: readonly number[]): number {
  return numbers.reduce((total, value) => total + value, 0);
}

function rowOf(listed: ListedSource, streams: readonly StreamStatus[]): SourceRow {
  return {
    name: listed.name,
    kind: listed.kind === "" ? (streams[0]?.sourceKind ?? "") : listed.kind,
    scheduleSeconds: listed.scheduleSeconds ?? streams[0]?.scheduleSeconds ?? null,
    streams: streams.length,
    records: sum(streams.map((stream) => stream.records)),
    notIndexed: sum(streams.map((stream) => stream.recordsNotIndexed)),
    freshness: worstOf(streams),
  };
}

/**
 * One workspace's overview. `listed` are its sources by the sources route, or null when that
 * route did not answer: the sources are then the ones the status has streams of.
 */
export function overviewOf(
  workspace: WorkspaceStatus,
  listed: readonly ListedSource[] | null,
): WorkspaceOverview {
  const seen = Array.from(new Set(workspace.streams.map((stream) => stream.source)));
  const known = listed ?? [];
  const unlisted = seen
    .filter((name) => !known.some((source) => source.name === name))
    .map((name): ListedSource => ({ name, kind: "", scheduleSeconds: null }));
  const sources = [...known, ...unlisted].sort((a, b) => a.name.localeCompare(b.name));
  return {
    name: workspace.name,
    records: workspace.index.records,
    chunks: workspace.index.chunks,
    notIndexed: workspace.index.recordsNotIndexed,
    index: workspaceIndexVerdict(workspace.index),
    sources: sources.map((source) =>
      rowOf(
        source,
        workspace.streams.filter((stream) => stream.source === source.name),
      ),
    ),
  };
}
