import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import type { Project } from "./catalog";
import { describeRefusal, fieldWords, isNameTaken } from "./save-refusal";

const draft: Project = {
  name: "port",
  title: "",
  description: "",
  version: 4,
  updatedAt: null,
  services: [
    { name: "berthing", origin: "berthing", description: "", repository: "harbour-code", root: "berthing", files: 1, uses: [] },
    { name: "", origin: null, description: "", repository: "harbour-code", root: "", files: null, uses: [] },
  ],
  domains: [{ name: "berths", title: "", description: "", code: [], subdomains: [] }],
};

/** A refusal of the server, as the Rust side hands it on. */
function refused(code: string, message: string, status = 409): AppError {
  return new AppError({ kind: "api", message, status, code });
}

describe("fieldWords", () => {
  it.each([
    ["services[0].root", "the directory of service berthing"],
    ["services[0].repository", "the repository of service berthing"],
    ["services[1].name", "the name of service 2"],
    ["services[0]", "service berthing"],
    ["domains[0].title", "the title of domain berths"],
    ["domains[0].subdomains[1].name", "the subdomains of domain berths"],
    ["services[0].other", "service berthing"],
    ["title", "title"],
  ])("says the field %s as %s", (field, words) => {
    expect(fieldWords(field, draft)).toBe(words);
  });
});

describe("describeRefusal", () => {
  it("says that a directory is a service of another project already", () => {
    const refusal = describeRefusal(refused("service_conflict", "A directory of a repository is one service in the workspace."), draft);

    expect([refusal?.title, refusal?.said]).toEqual(["That directory is already a service", null]);
  });

  it("says that another project names a removed service, with the server's words", () => {
    const said = "The document removes a service that a domain of another project names code in. Change that project first, or keep the service.";

    const refusal = describeRefusal(refused("service_in_use", said), draft);

    expect([refusal?.title, refusal?.said]).toEqual(["Another project names a service you removed or renamed", said]);
  });

  it("names the service whose directory the server did not accept, and gives its reason", () => {
    const said = "The project document is not valid: services[0].root: a root is a directory of the repository, from its root.";

    const refusal = describeRefusal(refused("invalid_project", said, 422), draft);

    expect(refusal).toEqual({
      title: "The server did not accept the project",
      text: "Not valid: the directory of service berthing. Correct it and save again.",
      said: "a root is a directory of the repository, from its root",
    });
  });

  it("shows a refusal it cannot read as the server said it", () => {
    const refusal = describeRefusal(refused("invalid_project", "Something else.", 422), draft);

    expect([refusal?.text, refusal?.said]).toEqual(["Correct it and save again.", "Something else."]);
  });

  it.each([
    ["not_found", 404, "The server does not know something the project names"],
    ["catalog_conflict", 409, "The catalog changed while the project was saved"],
  ])("describes the refusal %s", (code, status, title) => {
    expect(describeRefusal(refused(code, "The server's words.", status), draft)?.title).toBe(title);
  });

  it.each([
    ["a version conflict, which has its own answer", refused("version_conflict", "Changed.")],
    ["an error that is not a refusal of the catalog", new AppError({ kind: "unreachable", message: "No answer." })],
    ["no error", null],
  ])("leaves %s to others", (_case, error) => {
    expect(describeRefusal(error, draft)).toBeNull();
  });
});

describe("isNameTaken", () => {
  it.each([
    ["the server found a project of that name", refused("version_conflict", "The project exists."), true],
    ["the server refused for another reason", refused("invalid_project", "Not valid.", 422), false],
    ["there is no error", null, false],
  ])("tells when %s", (_case, error, expected) => {
    expect(isNameTaken(error)).toBe(expected);
  });
});
