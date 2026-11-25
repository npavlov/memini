// Edits of a project of the catalog, as pure changes of a draft. The draft is the whole project;
// saving writes all of it back with the version it was read at.

import { toProjectDocument } from "./catalog";
import type { Code, Domain, Project, Subdomain } from "./catalog";

/** Where a domain or a subdomain is in a project, by position. */
export interface EntryPlace {
  readonly domain: number;
  /** The position among the domain's subdomains; null for the domain itself. */
  readonly subdomain: number | null;
}

const NAME = /^[a-z0-9][a-z0-9_-]{0,62}$/u;

function replaceAt<T>(items: readonly T[], position: number, item: T): readonly T[] {
  return items.map((existing, index) => (index === position ? item : existing));
}

function editDomain(project: Project, position: number, change: (domain: Domain) => Domain): Project {
  const domain = project.domains[position];
  if (domain === undefined) {
    return project;
  }
  return { ...project, domains: replaceAt(project.domains, position, change(domain)) };
}

/** Changes the name, the title, the description or the code of a domain or a subdomain. */
export function editEntry(project: Project, place: EntryPlace, change: Partial<Subdomain>): Project {
  return editDomain(project, place.domain, (domain) => {
    const at = place.subdomain;
    if (at === null) {
      return { ...domain, ...change };
    }
    const subdomain = domain.subdomains[at];
    return subdomain === undefined
      ? domain
      : { ...domain, subdomains: replaceAt(domain.subdomains, at, { ...subdomain, ...change }) };
  });
}

/** The domain or the subdomain at a place; undefined when there is none. */
export function entryAt(project: Project, place: EntryPlace): Subdomain | undefined {
  const domain = project.domains[place.domain];
  return place.subdomain === null ? domain : domain?.subdomains[place.subdomain];
}

/** The path masks of a text: one per line, without the white space around them, empty lines dropped. */
export function masksOf(text: string): readonly string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

/** Which code of an entry a change of masks is for. */
export interface CodePlace extends EntryPlace {
  readonly code: number;
}

/**
 * Sets the path masks of one piece of code from the text of its field. The lines are kept as
 * they were typed, an empty one included, so the field does not change under the cursor;
 * `tidyProject` cleans them for saving.
 */
export function setMasks(project: Project, place: CodePlace, text: string): Project {
  const entry = entryAt(project, place);
  const code = entry?.code[place.code];
  if (entry === undefined || code === undefined) {
    return project;
  }
  const paths = text.split("\n");
  return editEntry(project, place, { code: replaceAt(entry.code, place.code, { ...code, paths }) });
}

/** The services of the project whose code an entry does not name yet. */
export function unattachedServices(project: Project, place: EntryPlace): readonly string[] {
  const attached = (entryAt(project, place)?.code ?? []).filter((code) => code.project === null);
  return project.services
    .map((service) => service.name)
    .filter((name) => !attached.some((code) => code.service === name));
}

/** Attaches code of a service of the project to a domain or a subdomain, with no mask yet. */
export function addCode(project: Project, place: EntryPlace, service: string): Project {
  const entry = entryAt(project, place);
  if (entry === undefined || service === "") {
    return project;
  }
  const added: Code = { service, project: null, paths: [] };
  return editEntry(project, place, { code: [...entry.code, added] });
}

const NEW_ENTRY: Subdomain = { name: "", title: "", description: "", code: [] };

export function addDomain(project: Project): Project {
  return { ...project, domains: [...project.domains, { ...NEW_ENTRY, subdomains: [] }] };
}

export function addSubdomain(project: Project, domainAt: number): Project {
  return editDomain(project, domainAt, (domain) => ({
    ...domain,
    subdomains: [...domain.subdomains, NEW_ENTRY],
  }));
}

/** Removes a domain with its subdomains, or one subdomain. The services stay. */
export function removeEntry(project: Project, place: EntryPlace): Project {
  const at = place.subdomain;
  if (at === null) {
    return { ...project, domains: project.domains.filter((_domain, index) => index !== place.domain) };
  }
  return editDomain(project, place.domain, (domain) => ({
    ...domain,
    subdomains: domain.subdomains.filter((_subdomain, index) => index !== at),
  }));
}

function tidyEntry<T extends Subdomain>(entry: T): T {
  const code = entry.code
    .map((item) => ({ ...item, paths: masksOf(item.paths.join("\n")) }))
    .filter((item) => item.paths.length > 0);
  return { ...entry, name: entry.name.trim(), title: entry.title.trim(), code };
}

/**
 * The draft as it is saved: names and titles without the white space around them, masks one per
 * line, and code that was left without a mask taken out.
 */
export function tidyProject(project: Project): Project {
  return {
    ...project,
    title: project.title.trim(),
    domains: project.domains.map((domain) => ({
      ...tidyEntry(domain),
      subdomains: domain.subdomains.map(tidyEntry),
    })),
  };
}

/** Whether saving the draft would write what the project already says. */
export function sameProject(draft: Project, project: Project): boolean {
  return JSON.stringify(toProjectDocument(tidyProject(draft))) === JSON.stringify(toProjectDocument(project));
}

function checkNames(names: readonly string[], what: string): string | null {
  if (names.some((name) => !NAME.test(name.trim()))) {
    return `Every ${what} needs a name of lowercase letters, digits, '-' or '_', at most 63 characters.`;
  }
  const repeated = names.find((name, index) => names.indexOf(name) !== index);
  return repeated === undefined ? null : `Two ${what}s are named ${repeated}.`;
}

/** The first problem with the draft, or null. A help for the user; the server checks again. */
export function checkProject(project: Project): string | null {
  const names = (entries: readonly Subdomain[]): readonly string[] => entries.map((entry) => entry.name);
  const problems = [
    checkNames(names(project.domains), "domain"),
    ...project.domains.map((domain) => checkNames(names(domain.subdomains), "subdomain")),
  ];
  return problems.find((problem) => problem !== null) ?? null;
}
