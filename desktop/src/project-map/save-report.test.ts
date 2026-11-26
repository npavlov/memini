import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import { describeSave, isVersionConflict, parseSaveReport } from "./save-report";
import type { SaveReport } from "./save-report";

/** What `put_project` answers. */
const answer = {
  workspace: "harbor",
  project: "port",
  version: 5,
  created: false,
  changed: true,
  redacted: { title: 1, description: 1 },
  masks: [],
  empty_masks: [{ domain: "berths/holds", project: "port", service: "berthing", path: "src/hold.py", files: 0 }],
  overlaps: [{ domains: ["berths", "fees"], source: "harbour-code", files: 2, examples: ["src/a.py"] }],
  services: [
    { service: "berthing", source: "harbour-code", files: 40, unassigned: 12, unassigned_share: 0.3 },
    { service: "fees", source: "harbour-code", files: 8, unassigned: 0, unassigned_share: 0 },
  ],
};

const clean: SaveReport = {
  version: 5,
  created: false,
  changed: true,
  redacted: 0,
  emptyMasks: [],
  overlaps: [],
  services: [],
};

describe("parseSaveReport", () => {
  it("reads the version and what the masks match", () => {
    const report = parseSaveReport(answer);

    expect(report).toEqual<SaveReport>({
      version: 5,
      created: false,
      changed: true,
      redacted: 2,
      emptyMasks: [{ domain: "berths/holds", service: "berthing", path: "src/hold.py" }],
      overlaps: [{ domains: ["berths", "fees"], source: "harbour-code", files: 2 }],
      services: [
        { service: "berthing", files: 40, unassigned: 12 },
        { service: "fees", files: 8, unassigned: 0 },
      ],
    });
  });

  it("reads an answer that says only the version", () => {
    const report = parseSaveReport({ version: 5, changed: true });

    expect(report).toEqual(clean);
  });
});

describe("describeSave", () => {
  it.each([
    [clean, "Saved as version 5."],
    [{ ...clean, created: true }, "The project was created as version 5."],
    [{ ...clean, changed: false }, "Nothing changed: the project already says this (version 5)."],
    [{ ...clean, version: null }, "Saved."],
  ])("says what was written for %j", (report, title) => {
    const summary = describeSave(report);

    expect([summary.tone, summary.title, summary.lines]).toEqual(["good", title, []]);
  });

  it("warns about masks that match nothing and domains that share files", () => {
    const summary = describeSave(parseSaveReport(answer));

    expect(summary).toEqual({
      tone: "warn",
      title: "Saved as version 5.",
      lines: [
        "berths/holds: the mask src/hold.py of berthing matches no file.",
        "berths and fees both name 2 files of harbour-code.",
        "2 secrets in titles and descriptions were masked before saving.",
        "berthing: 12 of 40 files are in no domain.",
      ],
    });
  });
});

describe("isVersionConflict", () => {
  it.each([
    ["the server's code for it", new AppError({ kind: "api", message: "Changed.", status: 409, code: "version_conflict" }), true],
    ["another error of the server", new AppError({ kind: "api", message: "Invalid.", status: 422, code: "invalid_project" }), false],
    ["another conflict", new AppError({ kind: "api", message: "In use.", status: 409, code: "service_in_use" }), false],
    ["no error", null, false],
  ])("tells %s", (_case, error, expected) => {
    expect(isVersionConflict(error)).toBe(expected);
  });
});
