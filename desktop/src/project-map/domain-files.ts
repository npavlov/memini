// How many files of the index the code of a domain matches now, from `get_domain`. The catalog
// says which masks a domain has; this says what they match.

import type { Backend } from "../ipc/backend";
import { readArray, readNumber, readObject, readString } from "../json/read-json";
import type { JsonObject } from "../json/read-json";
import type { Code, ProjectPlace } from "./catalog";

export interface CodeFiles {
  readonly project: string;
  readonly service: string;
  /** Files one of the masks matches, each once. */
  readonly files: number | null;
  /** Files by mask. */
  readonly masks: ReadonlyMap<string, number>;
}

export interface EntryFiles {
  /** Files the masks of the entry match, each once. */
  readonly files: number | null;
  readonly code: readonly CodeFiles[];
}

/** The files of a domain, and of each of its subdomains by name. */
export interface DomainFiles extends EntryFiles {
  readonly subdomains: ReadonlyMap<string, EntryFiles>;
}

function parseMask(value: unknown): readonly [string, number] | null {
  const mask = readObject(value, "a path mask of a domain");
  const path = readString(mask["path"]);
  const files = readNumber(mask["files"]);
  return path === null || files === null ? null : [path, files];
}

function parseCode(value: unknown): CodeFiles {
  const code = readObject(value, "the code of a domain");
  const masks = readArray(code["paths"])
    .map(parseMask)
    .filter((mask) => mask !== null);
  return {
    project: readString(code["project"]) ?? "",
    service: readString(code["service"]) ?? "",
    files: readNumber(code["files"]),
    masks: new Map(masks),
  };
}

function parseEntry(entry: JsonObject): EntryFiles {
  return { files: readNumber(entry["files"]), code: readArray(entry["code"]).map(parseCode) };
}

export function parseDomainFiles(value: unknown): DomainFiles {
  const domain = readObject(value, "a domain of the catalog");
  const subdomains = readArray(domain["subdomains"]).map((item): readonly [string, EntryFiles] => {
    const subdomain = readObject(item, "a subdomain of the catalog");
    return [readString(subdomain["name"]) ?? "", parseEntry(subdomain)];
  });
  return { ...parseEntry(domain), subdomains: new Map(subdomains) };
}

/** Which mask of which code a count is asked for. `project` is the project the entry is in. */
export interface MaskPlace {
  readonly project: string;
  readonly code: Code;
  readonly path: string;
}

/** How many files a mask matches; null when the answer does not say. */
export function maskFiles(entry: EntryFiles | null, place: MaskPlace): number | null {
  const project = place.code.project ?? place.project;
  const code = entry?.code.find((item) => item.service === place.code.service && item.project === project);
  return code?.masks.get(place.path) ?? null;
}

/** Which domain to read: a project of a workspace, and the domain's name. */
export interface DomainPlace extends ProjectPlace {
  readonly project: string;
  readonly domain: string;
}

export async function readDomainFiles(backend: Backend, place: DomainPlace): Promise<DomainFiles> {
  const asked = { workspace: place.workspace, project: place.project, domain: place.domain };
  return parseDomainFiles(await backend.invoke("api_domain", { id: place.id, place: asked }));
}
