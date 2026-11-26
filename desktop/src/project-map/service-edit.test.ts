import { describe, expect, it } from "vitest";

import { toProjectDocument } from "./catalog";
import type { Project, Service } from "./catalog";
import { checkNames, isName } from "./names";
import {
  addService,
  checkServices,
  editService,
  namedServices,
  removalQuestion,
  removeService,
  repositoryChoices,
  serviceDependents,
  shownService,
  tidyServices,
} from "./service-edit";

function service(name: string, changes: Partial<Service>): Service {
  return { name, origin: name, description: "", source: "harbour-code", root: name, files: 10, uses: [], ...changes };
}

/** A project with two services: a domain has code in both, and one service uses the other. */
const port: Project = {
  name: "port",
  title: "Port operations",
  description: "",
  version: 4,
  updatedAt: null,
  services: [
    service("berthing", { uses: [{ project: null, service: "fees", kind: "library", note: "", paths: [] }] }),
    service("fees", { uses: [{ project: "tides", service: "tables", kind: "http", note: "", paths: [] }] }),
  ],
  domains: [
    {
      name: "berths",
      title: "Berths",
      description: "",
      code: [
        { project: null, service: "berthing", paths: ["src/**"] },
        { project: null, service: "fees", paths: ["berth.py"] },
      ],
      subdomains: [{ name: "holds", title: "", description: "", code: [{ project: null, service: "fees", paths: ["hold.py"] }] }],
    },
  ],
};

describe("names", () => {
  it.each([
    ["port", true],
    ["harbour-code_2", true],
    [" port ", true],
    ["Port", false],
    ["-port", false],
    ["", false],
    ["a".repeat(64), false],
  ])("says whether %j is a name: %j", (text, expected) => {
    expect(isName(text)).toBe(expected);
  });

  it("finds two things of one name, whatever the white space around it", () => {
    expect(checkNames(["fees", " fees "], "service")).toBe("Two services are named fees.");
  });
});

describe("editService", () => {
  it("changes one service and leaves what names it as it is until the draft is saved", () => {
    const draft = editService(port, 1, { name: "tariffs", root: "tariffs" });

    expect([draft.services[1]?.name, draft.services[1]?.origin, draft.domains]).toEqual(["tariffs", "fees", port.domains]);
  });

  it("changes nothing for a service that is not there", () => {
    expect(editService(port, 7, { name: "x" }).services).toEqual(port.services);
  });
});

describe("tidyServices", () => {
  it("gives the code of the domains and the links the new name of a renamed service", () => {
    const tidy = tidyServices(editService(port, 1, { name: " tariffs " }));

    expect([
      tidy.services[1]?.name,
      tidy.services[0]?.uses,
      tidy.domains[0]?.code.map((code) => code.service),
      tidy.domains[0]?.subdomains[0]?.code[0]?.service,
    ]).toEqual(["tariffs", [{ project: null, service: "tariffs", kind: "library", note: "", paths: [] }], ["berthing", "tariffs"], "tariffs"]);
  });

  it("lets two services swap their names without their code going astray", () => {
    const swapped = editService(editService(port, 0, { name: "fees" }), 1, { name: "berthing" });

    const tidy = tidyServices(swapped);

    expect(tidy.domains[0]?.code).toEqual([
      { project: null, service: "fees", paths: ["src/**"] },
      { project: null, service: "berthing", paths: ["berth.py"] },
    ]);
  });

  it("leaves a link to a service of another project as it is, also when a service here has that name", () => {
    const tidy = tidyServices(editService(port, 0, { name: "tables" }));

    expect(tidy.services[1]?.uses).toEqual([{ project: "tides", service: "tables", kind: "http", note: "", paths: [] }]);
  });

  it.each([
    ["/services/fees/", "services/fees"],
    ["  fees ", "fees"],
    ["/", ""],
  ])("stores the directory %j as %j", (root, stored) => {
    expect(tidyServices(editService(port, 1, { root })).services[1]?.root).toBe(stored);
  });
});

describe("addService", () => {
  it("adds a service without a name, for the whole of the repository, that nothing names yet", () => {
    const draft = addService(port, "pilot-notes");

    expect(draft.services[2]).toEqual({
      name: "",
      origin: null,
      description: "",
      source: "pilot-notes",
      root: "",
      files: null,
      uses: [],
    });
  });

  it("sends no links for a new service to a server that keeps none", () => {
    const old = { ...port, services: port.services.map((item) => ({ ...item, uses: null })) };

    const draft = addService(old, "pilot-notes");

    expect(JSON.stringify(toProjectDocument(draft))).not.toContain("uses");
  });

  it("offers only the services that are saved, by the name they were read with", () => {
    const draft = editService(addService(port, "pilot-notes"), 2, { name: "pilots" });

    expect(namedServices(draft)).toEqual([
      { value: "berthing", label: "berthing" },
      { value: "fees", label: "fees" },
    ]);
  });
});

describe("removing a service", () => {
  it("counts what goes with it: the code of the domains in it, its links and the links to it", () => {
    expect(serviceDependents(port, 1)).toEqual({ code: 2, links: 2 });
  });

  it("says in the question what goes with it and what the server may refuse", () => {
    expect(removalQuestion(port, 1)).toBe(
      "Remove this service? With it go 2 code entries of this project’s domains and 2 links. It is gone from the server when you save, and the server refuses when another project names it.",
    );
  });

  it("says that nothing names a service that was just added", () => {
    const draft = addService(port, "pilot-notes");

    expect(removalQuestion(draft, 2)).toContain("Nothing else in this project names it.");
  });

  it("takes the service out with the code of the domains in it and the links to it", () => {
    const draft = removeService(port, 1);

    expect([
      draft.services.map((item) => item.name),
      draft.services[0]?.uses,
      draft.domains[0]?.code,
      draft.domains[0]?.subdomains[0]?.code,
    ]).toEqual([["berthing"], [], [{ project: null, service: "berthing", paths: ["src/**"] }], []]);
  });

  it("removes a service that was just added and nothing else", () => {
    const draft = removeService(addService(port, "pilot-notes"), 2);

    expect(draft).toEqual(port);
  });
});

describe("shownService", () => {
  it.each([
    ["a service of the project", { project: null, service: "fees" }, "tariffs"],
    ["a service of another project", { project: "tides", service: "fees" }, "tides/fees"],
    ["a service the project does not have", { project: null, service: "gone" }, "gone"],
  ])("names %s as it is named now", (_case, reference, expected) => {
    const draft = editService(port, 1, { name: "tariffs" });

    expect(shownService(draft, reference)).toBe(expected);
  });
});

describe("repositoryChoices", () => {
  it.each([
    ["a repository of the workspace", "harbour-code", ["harbour-code", "pilot-notes"]],
    ["one the catalog no longer lists", "old-code", ["old-code", "harbour-code", "pilot-notes"]],
  ])("offers the repositories of the workspace for a service in %s", (_case, current, expected) => {
    expect(repositoryChoices(["harbour-code", "pilot-notes"], current)).toEqual(expected);
  });
});

describe("checkServices", () => {
  it("finds nothing wrong with services as the server gave them", () => {
    expect(checkServices(port)).toBeNull();
  });

  it.each([
    ["a service named as another", { name: "berthing" }, "Two services are named berthing."],
    ["a service without a repository", { source: "" }, "Every service needs its repository."],
    ["a directory that leaves the repository", { root: "../fees" }, "The directory of a service is a path inside its repository, without '.', '..' or empty parts; empty for the whole repository."],
    ["a directory with an empty part", { root: "a//b" }, "The directory of a service is a path inside its repository, without '.', '..' or empty parts; empty for the whole repository."],
    ["the directory of another service", { root: "/berthing/" }, "Two services are the same directory of the same repository."],
  ])("names the problem of %s", (_case, change, expected) => {
    expect(checkServices(editService(port, 1, change))).toBe(expected);
  });

  it("accepts the whole repository as the directory of a service", () => {
    expect(checkServices(editService(port, 1, { root: "" }))).toBeNull();
  });
});
