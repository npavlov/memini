// The catalog of a workspace: its projects, their services, and their domains and subdomains
// with the code that belongs to them. Titles and descriptions are the server's data. They are
// kept here exactly as they came, because an edit writes the whole project back; they are made
// safe to show where they are shown.

import type { Backend, CommandArguments } from "../ipc/backend";
import {
  ShapeError,
  isObject,
  readArray,
  readNumber,
  readObject,
  readString,
  readStrings,
} from "../json/read-json";
import type { JsonObject } from "../json/read-json";

/** Code of a domain: path masks inside the root of one service. */
export interface Code {
  readonly service: string;
  /** The project of the service; null: the project the domain is in. */
  readonly project: string | null;
  readonly paths: readonly string[];
}

export interface Subdomain {
  readonly name: string;
  readonly title: string;
  readonly description: string;
  readonly code: readonly Code[];
}

export interface Domain extends Subdomain {
  readonly subdomains: readonly Subdomain[];
}

/** A service that a service uses. */
export interface Link {
  /** The project of the service used; null: the same project. */
  readonly project: string | null;
  readonly service: string;
  /** How it is used: a library, an HTTP API, a queue. */
  readonly kind: string;
  readonly note: string;
  readonly paths: readonly string[];
}

export interface Service {
  readonly name: string;
  /**
   * The name the service was read with; null for one added in the editor. What names a
   * service inside the project, the code of a domain or a link, keeps this name while the
   * project is edited, so a service can be renamed without losing them.
   */
  readonly origin: string | null;
  readonly description: string;
  readonly repository: string;
  /** Its directory in the repository; empty for the whole of it. */
  readonly root: string;
  /** Files of the service in the index; null when the answer does not say. */
  readonly files: number | null;
  /** The services it uses; null when the server does not keep links between services. */
  readonly uses: readonly Link[] | null;
}

export interface Project {
  readonly name: string;
  readonly title: string;
  readonly description: string;
  /** The version the project was read at: what a save is checked against. */
  readonly version: number;
  readonly updatedAt: string | null;
  readonly services: readonly Service[];
  readonly domains: readonly Domain[];
}

export interface Repository {
  readonly repository: string;
  readonly files: number | null;
}

export interface Catalog {
  readonly workspace: string;
  readonly projects: readonly Project[];
  /** Repositories of the workspace that no service names yet. */
  readonly unassigned: readonly Repository[];
}

function text(value: unknown): string {
  return readString(value) ?? "";
}

/** The project a service is named with, when it is not `own`: the answers always name one. */
function otherProject(value: unknown, own: string): string | null {
  const project = readString(value);
  return project === own ? null : project;
}

function parseCode(value: unknown, own: string): Code {
  const code = readObject(value, "the code of a domain");
  return {
    service: text(code["service"]),
    project: otherProject(code["project"], own),
    paths: readStrings(code["paths"]),
  };
}

function parseSubdomain(entry: JsonObject, own: string): Subdomain {
  return {
    name: text(entry["name"]),
    title: text(entry["title"]),
    description: text(entry["description"]),
    code: readArray(entry["code"]).map((code) => parseCode(code, own)),
  };
}

function parseDomain(value: unknown, own: string): Domain {
  const entry = readObject(value, "a domain of the catalog");
  const subdomains = readArray(entry["subdomains"]).map((item) =>
    parseSubdomain(readObject(item, "a subdomain of the catalog"), own),
  );
  return { ...parseSubdomain(entry, own), subdomains };
}

function parseLink(link: JsonObject, own: string): Link {
  return {
    project: otherProject(link["project"], own),
    service: text(link["service"]),
    kind: text(link["kind"]),
    note: text(link["note"]),
    paths: readStrings(link["paths"]),
  };
}

function parseService(value: unknown, own: string): Service {
  const entry = readObject(value, "a service of the catalog");
  const uses = entry["uses"];
  return {
    name: text(entry["name"]),
    origin: text(entry["name"]),
    description: text(entry["description"]),
    repository: text(entry["repository"]),
    root: text(entry["root"]),
    files: readNumber(entry["files"]),
    uses: Array.isArray(uses)
      ? readArray(uses)
          .filter(isObject)
          .map((link) => parseLink(link, own))
      : null,
  };
}

function parseProject(value: unknown): Project {
  const entry = readObject(value, "a project of the catalog");
  const version = readNumber(entry["version"]);
  if (version === null) {
    throw new ShapeError("a project with its version");
  }
  const name = text(entry["name"]);
  return {
    name,
    title: text(entry["title"]),
    description: text(entry["description"]),
    version,
    updatedAt: readString(entry["updated_at"]),
    services: readArray(entry["services"]).map((service) => parseService(service, name)),
    domains: readArray(entry["domains"]).map((domain) => parseDomain(domain, name)),
  };
}

export function parseCatalog(value: unknown): Catalog {
  const catalog = readObject(value, "the catalog of a workspace");
  return {
    workspace: text(catalog["workspace"]),
    projects: readArray(catalog["projects"]).map(parseProject),
    unassigned: readArray(catalog["unassigned_repositories"]).map((item) => {
      const repository = readObject(item, "a repository of the catalog");
      return { repository: text(repository["repository"]), files: readNumber(repository["files"]) };
    }),
  };
}

/** The git sources of the workspace, as the catalog names them: those of its services and those in no project. */
export function repositoriesOf(catalog: Catalog): readonly string[] {
  const used = catalog.projects.flatMap((project) => project.services.map((service) => service.repository));
  const names = [...used, ...catalog.unassigned.map((repository) => repository.repository)];
  return Array.from(new Set(names.filter((name) => name !== ""))).sort();
}

/** What the user enters for a new project. */
export interface NewProject {
  readonly name: string;
  readonly title: string;
  readonly description: string;
}

/**
 * A project that does not exist yet: empty, at version 0. Saving it sends `if_version: 0`, which
 * the server accepts only while no project has that name, so an existing one is never overwritten.
 */
export function newProject(entered: NewProject): Project {
  return {
    name: entered.name.trim(),
    title: entered.title.trim(),
    description: entered.description,
    version: 0,
    updatedAt: null,
    services: [],
    domains: [],
  };
}

/** A service as it is named where another project may be meant: `project/service`, or `service`. */
export function serviceLabel(place: { readonly project: string | null; readonly service: string }): string {
  return place.project === null ? place.service : `${place.project}/${place.service}`;
}

function codeDocument(code: Code): JsonObject {
  const own = { service: code.service, paths: code.paths };
  return code.project === null ? own : { ...own, project: code.project };
}

function subdomainDocument(entry: Subdomain): JsonObject {
  return {
    name: entry.name,
    title: entry.title,
    description: entry.description,
    code: entry.code.map(codeDocument),
  };
}

function linkDocument(link: Link): JsonObject {
  const own = { service: link.service, kind: link.kind, note: link.note, paths: link.paths };
  return link.project === null ? own : { ...own, project: link.project };
}

function serviceDocument(service: Service): JsonObject {
  const own = {
    name: service.name,
    description: service.description,
    repository: service.repository,
    root: service.root,
  };
  // Links are not edited here. The document replaces what is stored, so the links that were
  // read go back with it; a server without links is sent none.
  return service.uses === null ? own : { ...own, uses: service.uses.map(linkDocument) };
}

/**
 * The project as the document `put_project` takes. The document replaces what is stored, so it
 * holds everything the project has, and `if_version`: the version it was read at, which makes
 * the server refuse the write when someone changed the project meanwhile.
 */
export function toProjectDocument(project: Project): JsonObject {
  return {
    title: project.title,
    description: project.description,
    services: project.services.map(serviceDocument),
    domains: project.domains.map((domain) => ({
      ...subdomainDocument(domain),
      subdomains: domain.subdomains.map(subdomainDocument),
    })),
    if_version: project.version,
  };
}

/** Where a project is: the connection and the workspace it was read from. */
export interface ProjectPlace {
  readonly id: string;
  readonly workspace: string;
}

export async function readCatalog(backend: Backend, place: ProjectPlace): Promise<Catalog> {
  return parseCatalog(await backend.invoke("api_catalog", { id: place.id, workspace: place.workspace }));
}

/**
 * Writes the project and gives the server's answer. Rejects with the API's `version_conflict`
 * when the project changed after it was read: nothing is written then.
 */
export async function saveProject(backend: Backend, place: ProjectPlace, project: Project): Promise<unknown> {
  const write: CommandArguments = {
    workspace: place.workspace,
    project: project.name,
    document: toProjectDocument(project),
  };
  return backend.invoke("api_project_put", { id: place.id, write });
}
