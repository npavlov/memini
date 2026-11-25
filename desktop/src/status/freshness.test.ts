import { describe, expect, it } from "vitest";

import {
  behindReason,
  indexVerdict,
  runTone,
  streamVerdict,
  totals,
  workspaceIndexVerdict,
} from "./freshness";
import type { ServiceStatus, StreamStatus, WorkspaceIndex } from "./status";

function stream(changes: Partial<StreamStatus>): StreamStatus {
  return {
    source: "harbour-notes",
    sourceKind: "git",
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
    records: 0,
    recordsNotIndexed: 0,
    ...changes,
  };
}

function index(changes: Partial<WorkspaceIndex>): WorkspaceIndex {
  return {
    state: "ok",
    builtWith: null,
    records: 0,
    recordsNotIndexed: 0,
    chunks: 0,
    behind: false,
    reindex: null,
    ...changes,
  };
}

function status(streams: readonly StreamStatus[]): ServiceStatus {
  return {
    version: "0.2.0",
    asOf: null,
    current: false,
    databaseReachable: true,
    schemaVersion: 31,
    expectedSchemaVersion: 31,
    indexState: "ok",
    indexModel: null,
    indexNote: "",
    workspaces: [{ name: "harbour", streams, index: index({}) }],
  };
}

describe("behindReason", () => {
  it.each([
    ["never_checked", "never checked"],
    ["lag", "lagging"],
    ["not_indexed", "records not indexed"],
    ["index_stale", "index is stale"],
  ])("names the server's reason %s", (reason, words) => {
    expect(behindReason(reason)).toBe(words);
  });

  it.each([["a_new_reason"], ["constructor"], ["toString"]])(
    "shows the reason %s, which the app does not know, as it is",
    (reason) => {
      expect(behindReason(reason)).toBe(reason);
    },
  );
});

describe("streamVerdict", () => {
  it.each([
    ["scheduled and current", stream({}), "good", "current"],
    ["behind for two reasons", stream({ current: false, behind: ["lag", "not_indexed"] }), "bad", "behind: lagging, records not indexed"],
    ["behind without a reason", stream({ current: false }), "bad", "behind"],
    ["without a schedule", stream({ current: null, scheduleSeconds: null }), "idle", "on demand"],
    ["missing at the source", stream({ missing: true, current: false }), "warn", "missing at the source"],
  ])("judges a stream that is %s", (_what, given, tone, label) => {
    expect(streamVerdict(given)).toEqual({ tone, label });
  });
});

describe("runTone", () => {
  it.each([
    ["succeeded", "good"],
    ["failed", "bad"],
    ["partial", "warn"],
    ["running", "warn"],
    ["cancelled", "idle"],
    ["a_new_status", "idle"],
  ])("colours a run that %s", (runStatus, tone) => {
    expect(runTone(runStatus)).toBe(tone);
  });
});

describe("indexVerdict and workspaceIndexVerdict", () => {
  it.each([
    ["ok", "good"],
    ["stale", "bad"],
    ["none", "idle"],
    ["off", "idle"],
    ["unknown", "warn"],
  ])("judge an index whose state is %s", (state, tone) => {
    expect(indexVerdict(state).tone).toBe(tone);
  });

  it("show a state the app does not know as it is", () => {
    expect(indexVerdict("hasOwnProperty")).toEqual({ tone: "info", label: "hasOwnProperty" });
  });

  it("warn about a workspace whose index is ok but holds records that are not indexed", () => {
    expect(workspaceIndexVerdict(index({ behind: true }))).toEqual({
      tone: "warn",
      label: "records not indexed",
    });
  });
});

describe("totals", () => {
  it("counts the streams that are behind and those with failed runs", () => {
    const counted = totals(
      status([stream({}), stream({ current: false, behind: ["lag"] }), stream({ failures: 2 })]),
    );

    expect(counted).toEqual({ workspaces: 1, streams: 3, behind: 1, failing: 1 });
  });
});
