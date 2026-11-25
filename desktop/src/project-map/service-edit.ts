// Edits of the services of a project draft. What names a service inside the project, the code
// of a domain or a link of another service, names it by the name it was read with for as long
// as the draft is edited; the new name is put in when the draft is saved. So a service can be
// renamed letter by letter, and two can swap names, without a reference going astray.

import { countNoun } from "../text/count-noun";
import type { Project, Service, Subdomain } from "./catalog";
import { checkNames } from "./names";

export type ServiceChange = Partial<Pick<Service, "name" | "description" | "repository" | "root">>;

/** Something that names a service: the code of a domain, or a link. */
interface Reference {
  readonly project: string | null;
  readonly service: string;
}

export function editService(project: Project, at: number, change: ServiceChange): Project {
  const services = project.services.map((service, index) => (index === at ? { ...service, ...change } : service));
  return { ...project, services };
}

/** Adds a service without a name yet, in `repository`, for the whole of it. */
export function addService(project: Project, repository: string): Project {
  // A server that keeps no links sends none, and takes none.
  const linked = project.services.length === 0 || project.services.some((service) => service.uses !== null);
  const added: Service = {
    name: "",
    origin: null,
    description: "",
    repository,
    root: "",
    files: null,
    uses: linked ? [] : null,
  };
  return { ...project, services: [...project.services, added] };
}

function names(reference: Reference, origin: string | null): boolean {
  return origin !== null && reference.project === null && reference.service === origin;
}

function entriesOf(project: Project): readonly Subdomain[] {
  return project.domains.flatMap((domain) => [domain, ...domain.subdomains]);
}

/** What goes with a service when it is removed. */
export interface Dependents {
  /** The code entries of the project's domains and subdomains that are in the service. */
  readonly code: number;
  /** The links the service has, and the links of the project's other services to it. */
  readonly links: number;
}

export function serviceDependents(project: Project, at: number): Dependents {
  const service = project.services[at];
  if (service === undefined) {
    return { code: 0, links: 0 };
  }
  const origin = service.origin;
  const code = entriesOf(project)
    .flatMap((entry) => entry.code)
    .filter((item) => names(item, origin)).length;
  const others = project.services.filter((_other, index) => index !== at);
  const incoming = others.flatMap((other) => other.uses ?? []).filter((link) => names(link, origin)).length;
  return { code, links: incoming + (service.uses?.length ?? 0) };
}

/** What the app asks before a service is removed: what goes with it, and what the server may refuse. */
export function removalQuestion(project: Project, at: number): string {
  const { code, links } = serviceDependents(project, at);
  const goes =
    code + links === 0
      ? "Nothing else in this project names it."
      : `With it go ${countNoun(code, "code entry", "code entries")} of this project’s domains and ${countNoun(links, "link")}.`;
  return `Remove this service? ${goes} It is gone from the server when you save, and the server refuses when another project names it.`;
}

/** Removes a service with what names it inside the project: the code of domains, and links to it. */
export function removeService(project: Project, at: number): Project {
  const service = project.services[at];
  if (service === undefined) {
    return project;
  }
  const origin = service.origin;
  const without = <T extends Subdomain>(entry: T): T => ({
    ...entry,
    code: entry.code.filter((item) => !names(item, origin)),
  });
  const kept = project.services.filter((_other, index) => index !== at);
  return {
    ...project,
    services: kept.map((other) =>
      other.uses === null ? other : { ...other, uses: other.uses.filter((link) => !names(link, origin)) },
    ),
    domains: project.domains.map((domain) => ({ ...without(domain), subdomains: domain.subdomains.map(without) })),
  };
}

/** A service that code of a domain can be attached to. */
export interface NamedService {
  /** What a reference holds: the name the service was read with. */
  readonly value: string;
  /** The name as it is now. */
  readonly label: string;
}

/**
 * The services of the project that something can name: those that were read from the server. A
 * service added in the editor can be named after the project is saved with it.
 */
export function namedServices(project: Project): readonly NamedService[] {
  return project.services.flatMap((service) =>
    service.origin === null ? [] : [{ value: service.origin, label: service.name.trim() || service.origin }],
  );
}

/** The service a reference names, by its name as it is now; `project/service` for another project's. */
export function shownService(project: Project, reference: Reference): string {
  if (reference.project !== null) {
    return `${reference.project}/${reference.service}`;
  }
  const named = namedServices(project).find((service) => service.value === reference.service);
  return named?.label ?? reference.service;
}

/** The repositories a service can be given: those of the workspace, and the one it has when the catalog no longer lists it. */
export function repositoryChoices(repositories: readonly string[], current: string): readonly string[] {
  return current === "" || repositories.includes(current) ? repositories : [current, ...repositories];
}

/** A directory as the server stores it: without a slash at either end. */
function storedRoot(root: string): string {
  return root.trim().replace(/^\/+|\/+$/gu, "");
}

/**
 * The services as they are saved: names and directories tidied, and every reference inside the
 * project given the name its service has now.
 */
export function tidyServices(project: Project): Project {
  const renamed = new Map(
    project.services.flatMap((service): readonly (readonly [string, string])[] =>
      service.origin === null ? [] : [[service.origin, service.name.trim()]],
    ),
  );
  const follow = <T extends Reference>(reference: T): T =>
    reference.project === null
      ? { ...reference, service: renamed.get(reference.service) ?? reference.service }
      : reference;
  const tidyEntry = <T extends Subdomain>(entry: T): T => ({ ...entry, code: entry.code.map(follow) });
  return {
    ...project,
    services: project.services.map((service) => ({
      ...service,
      name: service.name.trim(),
      root: storedRoot(service.root),
      uses: service.uses === null ? null : service.uses.map(follow),
    })),
    domains: project.domains.map((domain) => ({ ...tidyEntry(domain), subdomains: domain.subdomains.map(tidyEntry) })),
  };
}

function isDirectory(root: string): boolean {
  const stored = storedRoot(root);
  const DELETE = 127;
  const controls = Array.from(stored).some((character) => {
    const code = character.codePointAt(0) ?? 0;
    return code < " ".charCodeAt(0) || code === DELETE;
  });
  return stored === "" || (!controls && stored.split("/").every((part) => part !== "" && part !== "." && part !== ".."));
}

/** The first problem with the services of the draft, or null. The server checks again. */
export function checkServices(project: Project): string | null {
  const named = checkNames(
    project.services.map((service) => service.name),
    "service",
  );
  if (named !== null) {
    return named;
  }
  if (project.services.some((service) => service.repository === "")) {
    return "Every service needs its repository.";
  }
  if (project.services.some((service) => !isDirectory(service.root))) {
    return "The directory of a service is a path inside its repository, without '.', '..' or empty parts; empty for the whole repository.";
  }
  const places = project.services.map((service) => `${service.repository}\n${storedRoot(service.root)}`);
  return places.some((place, index) => places.indexOf(place) !== index)
    ? "Two services are the same directory of the same repository."
    : null;
}
