import { describe, expect, it } from "vitest";

import { ShapeError } from "../json/read-json";
import type { StreamStatus, WorkspaceStatus } from "../status/status";
import { overviewOf, parseListedSources } from "./overview";

function stream(changes: Partial<StreamStatus>): StreamStatus {
  return {
    source: "harbour-notes",
    sourceKind: "repository",
    key: "refs/heads/main",
    scheduleSeconds: 3600,
    checkedUntil: null,
    lagSeconds: 60,
    current: true,
    behind: [],
    lastRun: null,
    failures: 0,
    lastFailure: null,
    missing: false,
    historyLoaded: null,
    records: 100,
    recordsNotIndexed: 0,
    ...changes,
  };
}

function workspace(streams: readonly StreamStatus[]): WorkspaceStatus {
  return {
    name: "harbour",
    streams,
    index: {
      state: "ok",
      builtWith: null,
      records: 150,
      recordsNotIndexed: 4,
      chunks: 900,
      behind: true,
      reindex: null,
      rebuild: null,
    },
  };
}

describe("parseListedSources", () => {
  it("reads the name, the kind and the schedule of each source", () => {
    const listed = parseListedSources({
      items: [{ name: "chat", kind: "push", schedule_seconds: null, location: null }],
    });

    expect(listed).toEqual([{ name: "chat", kind: "push", scheduleSeconds: null }]);
  });

  it("throws a ShapeError when the answer is not an object", () => {
    expect(() => parseListedSources([])).toThrow(ShapeError);
  });
});

describe("overviewOf", () => {
  it("counts a workspace's records and chunks from its index", () => {
    const overview = overviewOf(workspace([]), []);

    expect(overview).toMatchObject({
      name: "harbour",
      records: 150,
      chunks: 900,
      notIndexed: 4,
      index: { tone: "warn", label: "records not indexed" },
    });
  });

  it("adds up the streams of a source and takes the freshness of the worst one", () => {
    const streams = [
      stream({}),
      stream({ key: "refs/heads/next", records: 20, recordsNotIndexed: 3, current: false, behind: ["lag"] }),
    ];

    const row = overviewOf(workspace(streams), [
      { name: "harbour-notes", kind: "repository", scheduleSeconds: 3600 },
    ]).sources[0];

    expect(row).toEqual({
      name: "harbour-notes",
      kind: "repository",
      scheduleSeconds: 3600,
      streams: 2,
      records: 120,
      notIndexed: 3,
      freshness: { tone: "bad", label: "behind: lagging" },
    });
  });

  it("lists a source that has no stream yet, with nothing stored", () => {
    const row = overviewOf(workspace([]), [{ name: "chat", kind: "push", scheduleSeconds: null }])
      .sources[0];

    expect(row).toMatchObject({
      name: "chat",
      streams: 0,
      records: 0,
      freshness: { tone: "idle", label: "no stream yet" },
    });
  });

  it("takes the sources from the streams when the sources could not be listed", () => {
    const overview = overviewOf(workspace([stream({}), stream({ source: "chat", sourceKind: "push" })]), null);

    expect(overview.sources.map((row) => [row.name, row.kind])).toEqual([
      ["chat", "push"],
      ["harbour-notes", "repository"],
    ]);
  });
});
