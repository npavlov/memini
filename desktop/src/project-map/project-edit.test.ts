import { describe, expect, it } from "vitest";

import type { Project } from "./catalog";
import {
  addCode,
  addDomain,
  addSubdomain,
  checkProject,
  editEntry,
  entryAt,
  masksOf,
  removeEntry,
  sameProject,
  setMasks,
  tidyProject,
  unattachedServices,
} from "./project-edit";

const service = { description: "", repository: "harbour-code", files: 10, uses: null };

const port: Project = {
  name: "port",
  title: "Port operations",
  description: "",
  version: 4,
  updatedAt: null,
  services: [
    { ...service, name: "berthing", root: "berthing" },
    { ...service, name: "fees", root: "fees" },
  ],
  domains: [
    {
      name: "berths",
      title: "Berths",
      description: "",
      code: [{ project: null, service: "berthing", paths: ["src/**"] }],
      subdomains: [{ name: "holds", title: "Holds", description: "", code: [] }],
    },
    { name: "fees", title: "Fees", description: "", code: [], subdomains: [] },
  ],
};

const berths = { domain: 0, subdomain: null };
const holds = { domain: 0, subdomain: 0 };

describe("editEntry", () => {
  it("changes a domain and leaves its subdomains and the other domains", () => {
    const draft = editEntry(port, berths, { title: "Berths and quays" });

    expect([draft.domains[0]?.title, draft.domains[0]?.subdomains, draft.domains[1]]).toEqual([
      "Berths and quays",
      port.domains[0]?.subdomains,
      port.domains[1],
    ]);
  });

  it("changes a subdomain", () => {
    const draft = editEntry(port, holds, { description: "Held berths." });

    expect(entryAt(draft, holds)?.description).toBe("Held berths.");
  });

  it.each([
    ["a domain", { domain: 7, subdomain: null }],
    ["a subdomain", { domain: 0, subdomain: 7 }],
  ])("changes nothing for %s that is not there", (_case, place) => {
    const draft = editEntry(port, place, { title: "x" });

    expect(draft.domains).toEqual(port.domains);
  });
});

describe("path masks", () => {
  it("reads one mask per line and drops empty lines and the white space around a mask", () => {
    const masks = masksOf(" src/**\n\n  lib/berth.py \n");

    expect(masks).toEqual(["src/**", "lib/berth.py"]);
  });

  it("keeps the lines of a field as they were typed, so the field does not change under the cursor", () => {
    const draft = setMasks(port, { ...berths, code: 0 }, "src/**\n");

    expect(entryAt(draft, berths)?.code[0]?.paths).toEqual(["src/**", ""]);
  });

  it("changes nothing for code that is not there", () => {
    const draft = setMasks(port, { ...berths, code: 3 }, "x");

    expect(draft).toEqual(port);
  });

  it("attaches code of a service of the project with no mask yet", () => {
    const draft = addCode(port, holds, "fees");

    expect(entryAt(draft, holds)?.code).toEqual([{ project: null, service: "fees", paths: [] }]);
  });

  it.each([
    ["a domain with code in one service", berths, ["fees"]],
    ["a subdomain without code", holds, ["berthing", "fees"]],
  ])("offers the services not attached yet to %s", (_case, place, expected) => {
    const services = unattachedServices(port, place);

    expect(services).toEqual(expected);
  });
});

describe("domains and subdomains", () => {
  it("adds an empty domain at the end", () => {
    const draft = addDomain(port);

    expect(draft.domains[2]).toEqual({ name: "", title: "", description: "", code: [], subdomains: [] });
  });

  it("adds an empty subdomain to one domain", () => {
    const draft = addSubdomain(port, 1);

    expect(draft.domains.map((domain) => domain.subdomains.length)).toEqual([1, 1]);
  });

  it("removes a domain with its subdomains", () => {
    const draft = removeEntry(port, berths);

    expect(draft.domains.map((domain) => domain.name)).toEqual(["fees"]);
  });

  it("removes one subdomain and keeps its domain", () => {
    const draft = removeEntry(port, holds);

    expect([draft.domains[0]?.name, draft.domains[0]?.subdomains]).toEqual(["berths", []]);
  });
});

describe("tidyProject", () => {
  it("trims names, titles and masks, and takes out code that was left without a mask", () => {
    const typed = setMasks(editEntry(port, berths, { name: " berths ", title: " Berths " }), { ...berths, code: 0 }, "\n ");

    const tidy = tidyProject(addCode(typed, holds, "fees"));

    expect([tidy.domains[0]?.name, tidy.domains[0]?.title, tidy.domains[0]?.code, entryAt(tidy, holds)?.code]).toEqual([
      "berths",
      "Berths",
      [],
      [],
    ]);
  });

  it("says that a draft with only an empty line added is the project as it was", () => {
    const draft = setMasks(port, { ...berths, code: 0 }, "src/**\n");

    expect(sameProject(draft, port)).toBe(true);
  });

  it("says that a draft with another title is not the project as it was", () => {
    const draft = editEntry(port, berths, { title: "Quays" });

    expect(sameProject(draft, port)).toBe(false);
  });
});

describe("checkProject", () => {
  it("finds nothing wrong with a project as the server gave it", () => {
    expect(checkProject(port)).toBeNull();
  });

  it.each([
    ["a new domain without a name", addDomain(port), "Every domain needs a name of lowercase letters, digits, '-' or '_', at most 63 characters."],
    ["a domain named in capitals", editEntry(port, berths, { name: "Berths" }), "Every domain needs a name of lowercase letters, digits, '-' or '_', at most 63 characters."],
    ["two domains of one name", editEntry(port, berths, { name: "fees" }), "Two domains are named fees."],
    ["a new subdomain without a name", addSubdomain(port, 0), "Every subdomain needs a name of lowercase letters, digits, '-' or '_', at most 63 characters."],
  ])("names the problem of %s", (_case, draft, expected) => {
    expect(checkProject(draft)).toBe(expected);
  });
});
