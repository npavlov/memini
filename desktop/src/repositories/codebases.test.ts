import { describe, expect, it } from "vitest";

import {
  DEFAULT_PRESETS,
  SCOPE_PRESETS,
  agentTask,
  describeProgress,
  excludeOf,
  parsePreview,
  parseRunProgress,
  scopeOf,
  secondsLeft,
  splitGlobs,
} from "./codebases";

// The server takes at most 100 patterns per list (memini.sources.MAX_PATTERNS).
const SERVER_MAX_PATTERNS = 100;

describe("the scope of a code base", () => {
  it("leaves out tests, generated code, data and assets by default, within the server's limit", () => {
    const exclude = excludeOf(DEFAULT_PRESETS, []);

    expect(exclude).toContain("**/*_test.go");
    expect(exclude).toContain("**/src/test/**");
    expect(exclude).toContain("**/*.pb.go");
    expect(exclude).toContain("**/*.json");
    expect(exclude.length).toBeLessThanOrEqual(SERVER_MAX_PATTERNS - 20);
    expect(new Set(exclude).size).toBe(exclude.length);
  });

  it("finds the presets an exclude list holds and keeps the user's own globs", () => {
    const exclude = excludeOf(new Set(["tests"]), ["legacy/**", "  ", "**/*.json"]);

    const scope = scopeOf(exclude);

    expect([...scope.presets]).toEqual(["tests"]);
    expect(scope.custom).toEqual(["legacy/**", "**/*.json"]);
  });

  it("knows no preset in a list that holds only part of one", () => {
    expect(scopeOf(["**/*_test.go"]).presets.size).toBe(0);
  });

  it("reads one glob per line", () => {
    expect(splitGlobs(" legacy/**\r\n\n**/*.sql \n")).toEqual(["legacy/**", "**/*.sql"]);
  });

  it("gives each preset words and globs", () => {
    expect(SCOPE_PRESETS.every((preset) => preset.label !== "" && preset.globs.length > 0)).toBe(true);
  });
});

describe("the preview of a code base", () => {
  it("reads the totals and the groups", () => {
    const preview = parsePreview({
      tracked: 2726,
      indexed: 1472,
      skipped: { excluded: 1254 },
      by_extension: [{ key: ".go", tracked: 589, indexed: 400 }],
      by_folder: [{ key: "control-center", tracked: 1814, indexed: 900 }],
    });

    expect(preview).toEqual({
      tracked: 2726,
      indexed: 1472,
      byExtension: [{ key: ".go", tracked: 589, indexed: 400 }],
      byFolder: [{ key: "control-center", tracked: 1814, indexed: 900 }],
    });
  });
});

describe("the progress of a sync", () => {
  const run = (progress: unknown): unknown => ({ id: "r", status: "running", report: { details: { progress } } });
  const started = Date.parse("2026-11-25T20:00:00Z");

  it("reads the files of a running sync", () => {
    const progress = parseRunProgress(
      run({ files_total: 1472, files_processed: 312, files_committed: 300, started_at: "2026-11-25T20:00:00Z" }),
    );

    expect(progress).toEqual({ total: 1472, processed: 312, committed: 300, startedAt: started });
  });

  it("is null before the sync says how far it is", () => {
    expect(parseRunProgress({ id: "r", status: "running", report: { counts: {} } })).toBeNull();
    expect(parseRunProgress({ id: "r", status: "running", report: null })).toBeNull();
  });

  it("estimates what is left from the rate so far", () => {
    const progress = { total: 1000, processed: 250, committed: 200, startedAt: started };

    expect(secondsLeft(progress, started + 10 * 60_000)).toBe(30 * 60);
    expect(describeProgress(progress, started + 10 * 60_000)).toBe("250 of 1,000 files · about 30 minutes left");
  });

  it.each([
    ["nothing is done yet", { total: 10, processed: 0, committed: 0, startedAt: started }, started + 60_000],
    ["it has just started", { total: 10, processed: 2, committed: 0, startedAt: started }, started + 5_000],
    ["the start is not known", { total: 10, processed: 2, committed: 0, startedAt: null }, started + 60_000],
  ])("gives no estimate when %s", (_what, progress, now) => {
    expect(secondsLeft(progress, now)).toBeNull();
  });

  it("says it looks at changes while it has no files yet", () => {
    expect(describeProgress({ total: 0, processed: 0, committed: 0, startedAt: null }, 0)).toBe(
      "Looking at what changed…",
    );
  });
});

describe("the task for an agent", () => {
  it("names the workspace, its code bases and how to write the map", () => {
    const task = agentTask("harbour", [{ name: "berths-api", location: "/srv/local/berths-api" }]);

    expect(task).toContain('workspace "harbour"');
    expect(task).toContain("- berths-api (/srv/local/berths-api)");
    expect(task).toContain("catalog_guide");
    expect(task).toContain("put_project");
    expect(task).toContain("Markdown");
  });

  it("names a code base alone where its location is not known", () => {
    expect(agentTask("harbour", [{ name: "berths-api", location: null }])).toContain("- berths-api\n");
  });

  it("says to add a code base first when there is none", () => {
    expect(agentTask("harbour", [])).toContain("add one on the Codebases tab first");
  });
});
