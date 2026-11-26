import { describe, expect, it } from "vitest";

import {
  describeCheck,
  describeCoverage,
  describeHeld,
  describeOutcome,
  endedSyncs,
  parseCoverage,
  parseRunCheck,
  progressLabel,
  syncPhase,
} from "./codebases";
import { folderOf, parseRefStates, unusedFolders } from "./repositories";
import type { RefState } from "./repositories";

function run(steps: readonly string[], counts: Record<string, number>, details: unknown = null): unknown {
  return {
    id: "6f1c2b9e-0000-4000-8000-000000000001",
    status: "succeeded",
    report: { version: 1, steps: steps.map((name) => ({ name })), counts, details },
  };
}

describe("what a sync did", () => {
  it("reads a diff that changed nothing as a diff with no changes", () => {
    const check = parseRunCheck(run(["fetch", "diff", "index"], { files_unchanged: 0 }));

    expect(check.mode).toBe("diff");
    expect(check.progress).toBeNull();
    expect(describeCheck(check)).toBe("no changes");
  });

  it("counts what a diff added, changed and deleted", () => {
    const check = parseRunCheck(run(["diff", "index"], { files_added: 2, files_changed: 3, files_deleted: 1 }));

    expect(describeCheck(check)).toBe("2 added, 3 changed, 1 deleted");
  });

  it("reads a diff that fell back to listing the tree as a full listing", () => {
    const check = parseRunCheck(run(["diff", "list", "index"], { files_added: 1472, files_unchanged: 3 }));

    expect(check.mode).toBe("full");
    expect(describeCheck(check)).toBe("full listing: 1,472 files written, 3 unchanged");
  });

  it("knows neither mode for a run that failed before it planned", () => {
    const check = parseRunCheck({ id: "x", status: "failed", report: null });

    expect(check.mode).toBeNull();
    expect(progressLabel(check.mode)).toBe("Fetching the repository");
  });

  it("keeps the progress of a running full listing, and labels its bar", () => {
    const progress = { files_total: 10, files_processed: 4, files_committed: 2, started_at: "2026-11-26T07:00:00Z" };
    const check = parseRunCheck(run(["list"], {}, { progress }));

    expect(check.progress?.processed).toBe(4);
    expect(progressLabel(check.mode)).toBe("Full listing: every file of the branch");
    expect(progressLabel("diff")).toBe("Diff: what changed since the indexed commit");
  });
});

describe("what the index holds of a ref", () => {
  it("reads the files, chunks and commit of a stream, whatever its runs did", () => {
    const [main] = parseRefStates({
      items: [
        {
          key: "refs/heads/main",
          pending_sync: null,
          last_run: { id: "r1", status: "succeeded", finished_at: "2026-11-26T07:37:00Z", error: null },
          index: { records: 1472, chunks: 9310, commit: "1a2b3c4d5e6f", committed_at: "2026-11-25T14:02:00Z" },
        },
      ],
    });

    expect(main?.index).toEqual({ files: 1472, chunks: 9310, commit: "1a2b3c4d5e6f", committedAt: "2026-11-25T14:02:00Z" });
    expect(describeHeld(1472, 9310)).toBe("1,472 files · 9,310 chunks");
    expect(describeHeld(1, 1)).toBe("1 file · 1 chunk");
  });

  it("has no index state from a server that does not say, and refuses a commit that is not one", () => {
    const [old, odd] = parseRefStates({
      items: [
        { key: "refs/heads/main", pending_sync: null, last_run: null },
        { key: "refs/heads/dev", pending_sync: null, last_run: null, index: { records: 0, chunks: 0, commit: "--x" } },
      ],
    });

    expect(old?.index).toBeNull();
    expect(odd?.index?.commit).toBeNull();
  });
});

describe("how much of a code base the project map names", () => {
  const answer = {
    workspace: "harbour",
    source: "quay",
    files: 5,
    covered: 4,
    covered_share: 0.8,
    projects: [
      {
        project: "port",
        services: ["quay"],
        domains: ["fees/berth", "water"],
        files: 4,
        updated_at: "2026-11-26T07:00:00Z",
        updated_by: "p",
        updated_token: "t1",
      },
    ],
    uncovered_folders: [{ folder: "(root)", files: 1 }],
  };

  it("reads the projects, the share and the folders in no domain", () => {
    const coverage = parseCoverage(answer);

    expect(coverage.projects[0]?.domains).toEqual(["fees/berth", "water"]);
    expect(coverage.projects[0]?.updatedToken).toBe("t1");
    expect(coverage.uncovered).toEqual([{ folder: "(root)", files: 1 }]);
    expect(describeCoverage(coverage)).toBe("Mapped: 80% of 5 files are in a domain");
  });

  it.each([
    ["no project names it", { ...answer, covered: 0, covered_share: 0, projects: [] }, "Not mapped to the business yet"],
    ["nothing is indexed", { ...answer, files: 0, covered: 0, covered_share: null }, "Mapped; no file is indexed yet"],
  ])("says so when %s", (_when, value, said) => {
    expect(describeCoverage(parseCoverage(value))).toBe(said);
  });
});

describe("the folders of this computer", () => {
  const folder = { name: "quay", path: "/Users/sam/src/quay", location: "/srv/local/quay", branch: "main", present: true };
  const spare = { ...folder, name: "spare", path: "/Users/sam/src/spare", location: "/srv/local/spare" };

  it("finds the folder a code base reads by where the server sees it", () => {
    expect(folderOf([folder, spare], "/srv/local/quay/")).toEqual(folder);
    expect(folderOf([folder], "git@git.example.org:harbour/quay.git")).toBeNull();
  });

  it("leaves out of the Server screen's list the folders a code base of any workspace reads", () => {
    expect(unusedFolders([folder, spare], ["/srv/local/quay", "https://git.example.org/x.git"])).toEqual([spare]);
  });
});

describe("Sync now", () => {
  const format = { locale: "en-GB", timeZone: "UTC" };
  const noon = Date.parse("2026-11-26T12:30:00Z");
  const index = { records: 1472, chunks: 9310, commit: "0327a55e9b1c", committed_at: "2026-11-01T09:15:00Z" };
  const finished = { id: "run-2", status: "succeeded", error: null, finished_at: "2026-11-26T12:26:00Z" };

  function stream(fields: Record<string, unknown>): Record<string, unknown> {
    return { key: "refs/heads/main", pending_sync: null, last_run: null, index, ...fields };
  }

  function mainOf(fields: Record<string, unknown>): RefState {
    const [main] = parseRefStates({ items: [stream(fields)] });
    if (main === undefined) {
      throw new Error("no stream");
    }
    return main;
  }

  it.each([
    ["queued", { status: "queued", attempts: 0, last_error: null }, "info", "queued", true],
    ["running", { status: "running", attempts: 1, last_error: null }, "info", "syncing", false],
    ["queued again after a failed attempt", { status: "queued", attempts: 1, last_error: "fetch failed" }, "warn", "retrying", true],
  ])("labels a sync that is %s from the stream's job", (_state, job, tone, label, queued) => {
    const main = mainOf({ pending_sync: job, last_run: finished });

    expect([main.tone, main.label, main.queued]).toEqual([tone, label, queued]);
  });

  it("never says the sync waits once the stream has no job: its run says how it ended", () => {
    const main = mainOf({ last_run: finished });

    expect([main.label, main.queued, main.running]).toEqual(["synced", false, false]);
    expect(syncPhase([main])).toBeNull();
  });

  it("says that a sync without changes left the branch at its commit, and when that was made", () => {
    const check = parseRunCheck(run(["fetch", "diff", "index"], {}));

    expect(describeOutcome(mainOf({ last_run: finished }), check, noon, format)).toBe(
      "Checked 12:26 — no changes; main is still at 0327a55, committed 1 Nov",
    );
  });

  it("counts what a sync changed and names the commit it reached", () => {
    const check = parseRunCheck(run(["diff", "index"], { files_changed: 3, files_deleted: 1 }));

    expect(describeOutcome(mainOf({ last_run: finished }), check, noon, format)).toBe(
      "Checked 12:26 — 3 changed, 1 deleted; main is at 0327a55, committed 1 Nov",
    );
  });

  it("gives the reason of a sync that failed", () => {
    const main = mainOf({ last_run: { ...finished, status: "failed", error: "fetch failed" } });

    expect(describeOutcome(main, undefined, noon, format)).toBe("Checked 12:26 — failed: fetch failed");
  });

  it("has no outcome while a sync waits", () => {
    const main = mainOf({ pending_sync: { status: "queued", attempts: 0 }, last_run: finished });

    expect(describeOutcome(main, undefined, noon, format)).toBeNull();
  });

  it("knows a sync is under way while one ref waits and another runs", () => {
    const refs = parseRefStates({
      items: [
        stream({ key: "refs/heads/main", pending_sync: { status: "queued", attempts: 0 } }),
        stream({ key: "refs/heads/next", pending_sync: { status: "running", attempts: 1 } }),
      ],
    });

    expect(syncPhase(refs)).toBe("running");
    expect(syncPhase(refs.slice(0, 1))).toBe("queued");
  });

  it("ends the sync asked for when a new run finished, or every ref once it was seen under way", () => {
    const refs = parseRefStates({
      items: [
        stream({ key: "refs/heads/main", last_run: finished }),
        stream({ key: "refs/heads/next", last_run: { ...finished, id: "run-1" } }),
      ],
    });
    const before = new Map([
      ["refs/heads/main", "run-1"],
      ["refs/heads/next", "run-1"],
    ]);

    expect(endedSyncs(refs, before, false).map((ref) => ref.ref)).toEqual(["refs/heads/main"]);
    expect(endedSyncs(refs, before, true).map((ref) => ref.ref)).toEqual(["refs/heads/main", "refs/heads/next"]);
  });

  it("has not ended the sync asked for while it is still under way", () => {
    const refs = parseRefStates({ items: [stream({ pending_sync: { status: "running", attempts: 1 } })] });

    expect(endedSyncs(refs, new Map(), true)).toEqual([]);
  });
});
