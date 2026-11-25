import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { parseCatalog, readCatalog, saveProject, serviceLabel, toProjectDocument } from "./catalog";
import type { Catalog, Project } from "./catalog";

const STAMP = {
  updated_at: "2026-11-20T09:30:00Z",
  updated_by: "5b1d8a52-3c1f-4d0e-9a7b-2f4c6e8d0a11",
  updated_token: null,
};

/** The catalog as `get_catalog` answers, for the harbour of the fixtures. */
const answer = {
  workspace: "harbor",
  projects: [
    {
      name: "port",
      title: "Port operations",
      description: "Berths and fees.",
      version: 4,
      ...STAMP,
      services: [
        {
          name: "harbour-code",
          description: "Berth holds and their rules.",
          repository: "harbour-code",
          root: "",
          refs: [],
          files: 42,
          languages: [],
          ...STAMP,
        },
      ],
      domains: [
        {
          name: "berths",
          domain: "berths",
          title: "Berths",
          description: "",
          code: [{ project: "port", service: "harbour-code", paths: ["src/berth/**"], ...STAMP }],
          subdomains: [
            {
              name: "holds",
              domain: "berths/holds",
              title: "Berth holds",
              description: "",
              code: [{ project: "tides", service: "tables", paths: ["lib/hold.py"], ...STAMP }],
              ...STAMP,
            },
          ],
          ...STAMP,
        },
      ],
    },
  ],
  unassigned_repositories: [{ repository: "pilot-notes", refs: [], files: 7, languages: [] }],
};

const port: Project = {
  name: "port",
  title: "Port operations",
  description: "Berths and fees.",
  version: 4,
  updatedAt: "2026-11-20T09:30:00Z",
  services: [
    {
      name: "harbour-code",
      description: "Berth holds and their rules.",
      repository: "harbour-code",
      root: "",
      files: 42,
      uses: null,
    },
  ],
  domains: [
    {
      name: "berths",
      title: "Berths",
      description: "",
      code: [{ project: null, service: "harbour-code", paths: ["src/berth/**"] }],
      subdomains: [
        {
          name: "holds",
          title: "Berth holds",
          description: "",
          code: [{ project: "tides", service: "tables", paths: ["lib/hold.py"] }],
        },
      ],
    },
  ],
};

const document = {
  title: "Port operations",
  description: "Berths and fees.",
  services: [
    { name: "harbour-code", description: "Berth holds and their rules.", repository: "harbour-code", root: "" },
  ],
  domains: [
    {
      name: "berths",
      title: "Berths",
      description: "",
      code: [{ service: "harbour-code", paths: ["src/berth/**"] }],
      subdomains: [
        {
          name: "holds",
          title: "Berth holds",
          description: "",
          code: [{ service: "tables", paths: ["lib/hold.py"], project: "tides" }],
        },
      ],
    },
  ],
  if_version: 4,
};

function withUses(uses: unknown): unknown {
  const project = answer.projects[0];
  return { ...answer, projects: [{ ...project, services: [{ ...project?.services[0], uses }] }] };
}

describe("parseCatalog", () => {
  it("reads the projects with their services, domains and subdomains", () => {
    const catalog = parseCatalog(answer);

    expect(catalog).toEqual<Catalog>({
      workspace: "harbor",
      projects: [port],
      unassigned: [{ repository: "pilot-notes", files: 7 }],
    });
  });

  it("refuses a project that does not say its version, because a save is checked against it", () => {
    const parse = (): Catalog => parseCatalog({ projects: [{ name: "port" }] });

    expect(parse).toThrow("The answer is not a project with its version.");
  });

  it("reads the links between services when the server has them", () => {
    const uses = [{ project: "tides", service: "tables", kind: "http", note: "Tide times.", paths: ["src/tides.py"], ...STAMP }];

    const catalog = parseCatalog(withUses(uses));

    expect(catalog.projects[0]?.services[0]?.uses).toEqual([
      { project: "tides", service: "tables", kind: "http", note: "Tide times.", paths: ["src/tides.py"] },
    ]);
  });

  it("keeps text of the server as it came, markup included, because it is written back", () => {
    const catalog = parseCatalog({ projects: [{ name: "port", version: 1, title: "<b>Port</b>" }] });

    expect(catalog.projects[0]?.title).toBe("<b>Port</b>");
  });
});

describe("toProjectDocument", () => {
  it("is the whole project with the version it was read at", () => {
    const written = toProjectDocument(port);

    expect(written).toEqual(document);
  });

  it("sends no links to a server that has none", () => {
    const written = toProjectDocument(port);

    expect(JSON.stringify(written)).not.toContain("uses");
  });

  it("sends the links back as they were read, so a save does not remove them", () => {
    const linked = parseCatalog(
      withUses([{ project: "port", service: "fees", kind: "library", note: "", paths: [], ...STAMP }]),
    ).projects[0];

    const written = toProjectDocument(linked ?? port);

    expect(written["services"]).toEqual([
      {
        name: "harbour-code",
        description: "Berth holds and their rules.",
        repository: "harbour-code",
        root: "",
        uses: [{ service: "fees", kind: "library", note: "", paths: [] }],
      },
    ]);
  });
});

describe("serviceLabel", () => {
  it.each([
    [{ project: null, service: "harbour-code" }, "harbour-code"],
    [{ project: "tides", service: "tables" }, "tides/tables"],
  ])("names %j as %s", (place, expected) => {
    expect(serviceLabel(place)).toBe(expected);
  });
});

describe("the commands of the Rust side", () => {
  it("reads the catalog of one workspace through one command", async () => {
    const backend = createFakeBackend({ answers: { api_catalog: () => answer } });

    const catalog = await readCatalog(backend, { id: "c1", workspace: "harbor" });

    expect([catalog.projects, backend.calls]).toEqual([
      [port],
      [{ command: "api_catalog", args: { id: "c1", workspace: "harbor" } }],
    ]);
  });

  it("saves a project as one document that carries the version it was read at", async () => {
    const backend = createFakeBackend({ answers: { api_project_put: () => ({ version: 5 }) } });

    await saveProject(backend, { id: "c1", workspace: "harbor" }, port);

    expect(backend.calls).toEqual([
      { command: "api_project_put", args: { id: "c1", write: { workspace: "harbor", project: "port", document } } },
    ]);
  });
});
