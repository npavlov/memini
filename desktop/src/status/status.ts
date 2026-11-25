// The answer of `GET /v1/status`: whether the base is current, and each stream's freshness.
// Everything in it is the server's data. Names and error lines are sanitized here and shown as
// text; the reindex command is shown for the operator to read, never run by the app.

import type { Backend } from "../ipc/backend";
import {
  readArray,
  readBoolean,
  readNumber,
  readObject,
  readString,
  readStrings,
} from "../json/read-json";
import type { JsonObject } from "../json/read-json";
import { displayText } from "../text/display-text";

export interface StreamRun {
  readonly kind: string;
  readonly status: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly error: string | null;
}

export interface StreamStatus {
  readonly source: string;
  readonly sourceKind: string;
  readonly key: string;
  readonly scheduleSeconds: number | null;
  readonly checkedUntil: string | null;
  readonly lagSeconds: number | null;
  /** false: behind; true: scheduled and current; null: runs only when triggered. */
  readonly current: boolean | null;
  readonly behind: readonly string[];
  readonly lastRun: StreamRun | null;
  readonly failures: number;
  readonly lastFailure: StreamRun | null;
  readonly missing: boolean;
  readonly historyLoaded: boolean | null;
  readonly records: number;
  readonly recordsNotIndexed: number;
}

/** A rebuild of a workspace's index that waits or runs: after an upgrade the server starts one. */
export interface IndexRebuild {
  readonly state: "queued" | "running";
  /** Null until the running rebuild has counted the records. */
  readonly recordsDone: number | null;
  readonly recordsTotal: number | null;
  readonly percent: number | null;
}

export interface WorkspaceIndex {
  readonly state: string;
  readonly builtWith: string | null;
  readonly records: number;
  readonly recordsNotIndexed: number;
  readonly chunks: number;
  readonly behind: boolean;
  readonly reindex: string | null;
  readonly rebuild: IndexRebuild | null;
}

export interface WorkspaceStatus {
  readonly name: string;
  readonly streams: readonly StreamStatus[];
  readonly index: WorkspaceIndex;
}

export interface ServiceStatus {
  readonly version: string;
  readonly asOf: string | null;
  readonly current: boolean;
  readonly databaseReachable: boolean;
  readonly schemaVersion: number | null;
  readonly expectedSchemaVersion: number | null;
  readonly indexState: string;
  readonly indexModel: string | null;
  readonly indexNote: string;
  readonly workspaces: readonly WorkspaceStatus[];
}

function shown(value: unknown, limit?: number): string {
  return displayText(readString(value), limit);
}

function shownOrNull(value: unknown, limit?: number): string | null {
  const text = readString(value);
  return text === null ? null : displayText(text, limit);
}

function parseRun(value: unknown): StreamRun | null {
  if (value === null || value === undefined) {
    return null;
  }
  const run = readObject(value, "a run of a stream");
  return {
    kind: shown(run["kind"], 40),
    status: shown(run["status"], 20),
    startedAt: readString(run["started_at"]),
    finishedAt: readString(run["finished_at"]),
    error: shownOrNull(run["error"], 240),
  };
}

function parseStream(value: unknown): StreamStatus {
  const stream = readObject(value, "a stream of the status");
  return {
    source: shown(stream["source"], 80),
    sourceKind: shown(stream["source_kind"], 20),
    key: shown(stream["key"], 200),
    scheduleSeconds: readNumber(stream["schedule_seconds"]),
    checkedUntil: readString(stream["checked_until"]),
    lagSeconds: readNumber(stream["lag_seconds"]),
    current: readBoolean(stream["current"]),
    behind: readStrings(stream["behind"]).map((reason) => displayText(reason, 40)),
    lastRun: parseRun(stream["last_run"]),
    failures: readNumber(stream["failures"]) ?? 0,
    lastFailure: parseRun(stream["last_failure"]),
    missing: readBoolean(stream["missing"]) ?? false,
    historyLoaded: readBoolean(stream["history_loaded"]),
    records: readNumber(stream["records"]) ?? 0,
    recordsNotIndexed: readNumber(stream["records_not_indexed"]) ?? 0,
  };
}

function parseRebuild(value: unknown): IndexRebuild | null {
  if (value === null || value === undefined) {
    return null;
  }
  const rebuild = readObject(value, "the rebuild of an index");
  return {
    state: readString(rebuild["state"]) === "running" ? "running" : "queued",
    recordsDone: readNumber(rebuild["records_done"]),
    recordsTotal: readNumber(rebuild["records_total"]),
    percent: readNumber(rebuild["percent"]),
  };
}

function parseIndex(index: JsonObject): WorkspaceIndex {
  return {
    state: shown(index["state"], 20),
    builtWith: shownOrNull(index["built_with"], 120),
    records: readNumber(index["records"]) ?? 0,
    recordsNotIndexed: readNumber(index["records_not_indexed"]) ?? 0,
    chunks: readNumber(index["chunks"]) ?? 0,
    behind: readBoolean(index["behind"]) ?? false,
    reindex: shownOrNull(index["reindex"], 200),
    rebuild: parseRebuild(index["rebuild"]),
  };
}

function parseWorkspace(value: unknown): WorkspaceStatus {
  const workspace = readObject(value, "a workspace of the status");
  return {
    name: shown(workspace["name"], 80),
    streams: readArray(workspace["streams"]).map(parseStream),
    index: parseIndex(readObject(workspace["index"], "a workspace's index")),
  };
}

export function parseServiceStatus(value: unknown): ServiceStatus {
  const status = readObject(value, "the server's status");
  const database = readObject(status["database"], "the status of the database");
  const index = readObject(status["index"], "the status of the index");
  return {
    version: shown(status["version"], 40),
    asOf: readString(status["as_of"]),
    current: readBoolean(status["current"]) ?? false,
    databaseReachable: readBoolean(database["reachable"]) ?? false,
    schemaVersion: readNumber(database["schema_version"]),
    expectedSchemaVersion: readNumber(database["expected_schema_version"]),
    indexState: shown(index["state"], 20),
    indexModel: shownOrNull(index["configured"], 120),
    indexNote: shown(index["note"], 400),
    workspaces: readArray(status["workspaces"]).map(parseWorkspace),
  };
}

export async function readServiceStatus(backend: Backend, id: string): Promise<ServiceStatus> {
  return parseServiceStatus(await backend.invoke("api_status", { id }));
}

/** A workspace whose index is being rebuilt, with how far it is. */
export interface WorkspaceRebuild {
  readonly workspace: string;
  readonly rebuild: IndexRebuild;
}

/** The workspaces of the status whose index rebuild waits or runs, in the server's order. */
export function rebuildsOf(status: ServiceStatus): readonly WorkspaceRebuild[] {
  return status.workspaces.flatMap((workspace) =>
    workspace.index.rebuild === null ? [] : [{ workspace: workspace.name, rebuild: workspace.index.rebuild }],
  );
}

/** The words under a rebuild's bar: "312 of 1,472 records (21%)", or that it waits. */
export function describeRebuild(rebuild: IndexRebuild): string {
  if (rebuild.state === "queued") {
    return "Waiting to start";
  }
  if (rebuild.recordsDone === null || rebuild.recordsTotal === null) {
    return "Started";
  }
  const percent = rebuild.percent === null ? "" : ` (${String(rebuild.percent)}%)`;
  return `${rebuild.recordsDone.toLocaleString("en-US")} of ${rebuild.recordsTotal.toLocaleString("en-US")} records${percent}`;
}
