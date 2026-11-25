import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { maskFiles, parseDomainFiles, readDomainFiles } from "./domain-files";

/** A domain as `get_domain` answers. */
const answer = {
  workspace: "harbor",
  project: "port",
  name: "berths",
  domain: "berths",
  parent: null,
  title: "Berths",
  description: "",
  files: 12,
  code: [
    {
      project: "port",
      service: "berthing",
      repository: "harbour-code",
      root: "berthing",
      files: 9,
      paths: [
        { path: "src/**", files: 9 },
        { path: "docs/berth.md", files: 0 },
      ],
    },
  ],
  subdomains: [
    {
      name: "holds",
      domain: "berths/holds",
      title: "Holds",
      description: "",
      files: 3,
      code: [{ project: "tides", service: "tables", repository: "tide-tables", root: "", files: 3, paths: [{ path: "lib/hold.py", files: 3 }] }],
    },
  ],
};

const own = { project: null, service: "berthing", paths: ["src/**", "docs/berth.md"] };
const other = { project: "tides", service: "tables", paths: ["lib/hold.py"] };

describe("parseDomainFiles", () => {
  it("reads what the masks of a domain and of its subdomains match", () => {
    const files = parseDomainFiles(answer);

    expect([files.files, files.code[0]?.files, files.subdomains.get("holds")?.files]).toEqual([12, 9, 3]);
  });

  it("looks a subdomain up by its name only, whatever the server names it", () => {
    const files = parseDomainFiles({ subdomains: [{ name: "constructor", files: 1, code: [] }] });

    expect([files.subdomains.get("constructor")?.files, files.subdomains.get("toString")]).toEqual([1, undefined]);
  });
});

describe("maskFiles", () => {
  it.each([
    ["a mask that matches files", "src/**", 9],
    ["a mask that matches nothing", "docs/berth.md", 0],
    ["a mask the answer does not have", "lib/**", null],
  ])("counts %s", (_case, path, expected) => {
    const files = maskFiles(parseDomainFiles(answer), { project: "port", code: own, path });

    expect(files).toBe(expected);
  });

  it("counts a mask of a service of another project", () => {
    const holds = parseDomainFiles(answer).subdomains.get("holds") ?? null;

    const files = maskFiles(holds, { project: "port", code: other, path: "lib/hold.py" });

    expect(files).toBe(3);
  });

  it("has no count while the answer is not there", () => {
    const files = maskFiles(null, { project: "port", code: own, path: "src/**" });

    expect(files).toBeNull();
  });
});

describe("readDomainFiles", () => {
  it("asks for one domain of one project of one workspace", async () => {
    const backend = createFakeBackend({ answers: { api_domain: () => answer } });

    const files = await readDomainFiles(backend, { id: "c1", workspace: "harbor", project: "port", domain: "berths" });

    expect([files.files, backend.calls]).toEqual([
      12,
      [{ command: "api_domain", args: { id: "c1", place: { workspace: "harbor", project: "port", domain: "berths" } } }],
    ]);
  });
});
