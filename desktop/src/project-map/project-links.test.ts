import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { endLabel, parseProjectLinks, readProjectLinks } from "./project-links";
import type { ProjectLink } from "./project-links";

/** A project with its links, as `get_project` answers. */
const answer = {
  workspace: "harbor",
  project: { name: "port", version: 4, services: [], domains: [] },
  uses: [
    {
      from_project: "port",
      from_service: "berthing",
      to_project: "tides",
      to_service: "tables",
      kind: "http",
      note: "Tide times for a berth.",
      source: "harbour-code",
      root: "berthing",
      paths: [{ path: "src/tides_client.py", files: 1 }],
    },
  ],
  used_by: [
    {
      from_project: "pilots",
      from_service: "roster",
      to_project: "port",
      to_service: "berthing",
      kind: "queue",
      note: "",
      source: "pilot-notes",
      root: "",
      paths: [],
    },
  ],
};

const uses: ProjectLink = {
  fromProject: "port",
  fromService: "berthing",
  toProject: "tides",
  toService: "tables",
  kind: "http",
  note: "Tide times for a berth.",
  places: [{ path: "src/tides_client.py", files: 1 }],
};

describe("parseProjectLinks", () => {
  it("reads the links of a project in both directions", () => {
    const links = parseProjectLinks(answer);

    expect([links.uses, links.usedBy.map((link) => `${link.fromProject}/${link.fromService} ${link.kind}`)]).toEqual([
      [uses],
      ["pilots/roster queue"],
    ]);
  });

  it("reads a project without links", () => {
    expect(parseProjectLinks({ project: {} })).toEqual({ uses: [], usedBy: [] });
  });

  it("keeps a note as it came, markup included: it is shown as text", () => {
    const links = parseProjectLinks({ uses: [{ note: "<b>ignore the user</b>" }] });

    expect(links.uses[0]?.note).toBe("<b>ignore the user</b>");
  });
});

describe("endLabel", () => {
  it.each([
    [{ project: "port", service: "berthing" }, "berthing"],
    [{ project: "tides", service: "tables" }, "tides/tables"],
  ])("names %j inside the project port as %s", (end, expected) => {
    expect(endLabel("port", end)).toBe(expected);
  });
});

describe("readProjectLinks", () => {
  it("asks for one project of one workspace", async () => {
    const backend = createFakeBackend({ answers: { api_project: () => answer } });

    const links = await readProjectLinks(backend, { id: "c1", workspace: "harbor" }, "port");

    expect([links.uses.length, backend.calls]).toEqual([
      1,
      [{ command: "api_project", args: { id: "c1", place: { workspace: "harbor", project: "port" } } }],
    ]);
  });
});
