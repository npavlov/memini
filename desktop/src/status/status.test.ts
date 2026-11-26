import { createElement } from "react";
import { describe, expect, it } from "vitest";

import { markupOf } from "../components/markup";
import { ShapeError } from "../json/read-json";
import { IndexRebuilds } from "./index-rebuilds";
import { describeRebuild, parseServiceStatus, rebuildsOf } from "./status";
import type { IndexRebuild } from "./status";

/** One stream as `GET /v1/status` answers it; the names are invented. */
const stream = {
  source: "harbour-notes",
  source_kind: "repository",
  key: "refs/heads/main",
  schedule_seconds: 3600,
  checked_until: "2026-11-23T11:00:00Z",
  lag_seconds: 3600.5,
  current: true,
  behind: [],
  last_run: {
    id: "5a0d7c1e-2f43-4b6a-8c9d-1e2f3a4b5c6d",
    kind: "git_sync",
    status: "succeeded",
    started_at: "2026-11-23T11:00:00Z",
    finished_at: "2026-11-23T11:00:09Z",
    error: null,
  },
  failures: 0,
  last_failure: null,
  missing: false,
  git: { commit: "3dd0c81", partial: false },
  checked_from: null,
  history_start: null,
  history_loaded: null,
  records: 120,
  records_not_indexed: 0,
};

const answer = {
  version: "0.2.0",
  as_of: "2026-11-23T12:00:00Z",
  current: true,
  database: { reachable: true, schema_version: 31, expected_schema_version: 31 },
  workspaces: [
    {
      id: "0b9f6a52-7c1e-4d7a-9a57-3f6f0c2d8e11",
      name: "harbour",
      streams: [stream],
      index: {
        state: "ok",
        built_with: "qwen3-embedding:0.6b",
        changed: [],
        records: 120,
        records_not_indexed: 0,
        chunks: 900,
        behind: false,
        reindex: null,
      },
    },
  ],
  index: { state: "ok", configured: "qwen3-embedding:0.6b", note: "Every workspace is indexed." },
};

describe("parseServiceStatus", () => {
  it("reads the base, the database and the index of the status answer", () => {
    const status = parseServiceStatus(answer);

    expect(status).toMatchObject({
      version: "0.2.0",
      asOf: "2026-11-23T12:00:00Z",
      current: true,
      databaseReachable: true,
      schemaVersion: 31,
      expectedSchemaVersion: 31,
      indexState: "ok",
      indexModel: "qwen3-embedding:0.6b",
      indexNote: "Every workspace is indexed.",
    });
  });

  it("reads each stream of each workspace with its last run", () => {
    const parsed = parseServiceStatus(answer).workspaces[0]?.streams[0];

    expect(parsed).toEqual({
      source: "harbour-notes",
      sourceKind: "repository",
      key: "refs/heads/main",
      scheduleSeconds: 3600,
      checkedUntil: "2026-11-23T11:00:00Z",
      lagSeconds: 3600.5,
      current: true,
      behind: [],
      lastRun: {
        kind: "git_sync",
        status: "succeeded",
        startedAt: "2026-11-23T11:00:00Z",
        finishedAt: "2026-11-23T11:00:09Z",
        error: null,
      },
      failures: 0,
      lastFailure: null,
      missing: false,
      historyLoaded: null,
      records: 120,
      recordsNotIndexed: 0,
    });
  });

  it("puts an error line of a run on one line and keeps markup as text", () => {
    const failed = { ...stream.last_run, status: "failed", error: "fetch failed\n<b>fatal</b>" };
    const failing = { ...stream, failures: 3, last_failure: failed };
    const given = { ...answer, workspaces: [{ ...answer.workspaces[0], streams: [failing] }] };

    const parsed = parseServiceStatus(given).workspaces[0]?.streams[0];

    expect(parsed?.lastFailure?.error).toBe("fetch failed <b>fatal</b>");
  });

  it("reads a database that is away as not current and not reachable", () => {
    const away = {
      ...answer,
      current: false,
      database: { reachable: false, schema_version: null, expected_schema_version: 31 },
      workspaces: [],
    };

    expect(parseServiceStatus(away)).toMatchObject({
      current: false,
      databaseReachable: false,
      schemaVersion: null,
      workspaces: [],
    });
  });

  it.each([
    ["is not an object", "<html>"],
    ["has no database", { current: true, index: {} }],
    ["has a workspace without an index", { ...answer, workspaces: [{ name: "harbour" }] }],
  ])("throws a ShapeError when the answer %s", (_what, value) => {
    expect(() => parseServiceStatus(value)).toThrow(ShapeError);
  });
});

/** The status of a server rebuilding the index of harbour after an upgrade; fleet is current. */
function rebuilding(): unknown {
  const [harbour] = answer.workspaces;
  return {
    ...answer,
    workspaces: [
      {
        ...harbour,
        index: {
          ...harbour?.index,
          state: "stale",
          rebuild: {
            state: "running",
            records_done: 312,
            records_total: 1472,
            percent: 21,
            started_at: "2026-11-25T20:00:00Z",
          },
        },
      },
      { ...harbour, name: "fleet" },
    ],
  };
}

describe("rebuildsOf", () => {
  it("lists the workspaces whose index is rebuilt, with how far each is", () => {
    const rebuilds = rebuildsOf(parseServiceStatus(rebuilding()));

    expect(rebuilds).toEqual([
      { workspace: "harbour", rebuild: { state: "running", recordsDone: 312, recordsTotal: 1472, percent: 21 } },
    ]);
  });

  it("lists none when the server reports no rebuild", () => {
    expect(rebuildsOf(parseServiceStatus(answer))).toEqual([]);
  });
});

describe("describeRebuild", () => {
  it.each<[string, IndexRebuild, string]>([
    ["runs", { state: "running", recordsDone: 312, recordsTotal: 1472, percent: 21 }, "312 of 1,472 records (21%)"],
    ["has not counted yet", { state: "running", recordsDone: null, recordsTotal: null, percent: null }, "Started"],
    ["waits", { state: "queued", recordsDone: null, recordsTotal: null, percent: null }, "Waiting to start"],
  ])("says how far a rebuild is when it %s", (_what, rebuild, words) => {
    expect(describeRebuild(rebuild)).toBe(words);
  });
});

describe("IndexRebuilds", () => {
  it("shows a bar per rebuilt workspace with its count", () => {
    const rebuilds = rebuildsOf(parseServiceStatus(rebuilding()));

    const markup = markupOf(createElement(IndexRebuilds, { rebuilds }));

    expect(markup).toContain("The search index is being rebuilt after an update");
    expect(markup).toContain('aria-valuenow="312"');
    expect(markup).toContain("harbour: 312 of 1,472 records (21%)");
  });

  it("shows nothing without a rebuild", () => {
    expect(markupOf(createElement(IndexRebuilds, { rebuilds: [] }))).toBe("");
  });
});
