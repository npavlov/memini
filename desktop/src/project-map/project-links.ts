// The links of a project in both directions, from `get_project`: what its services use, and
// which services of other projects use them. They are shown and not edited here.

import type { Backend } from "../ipc/backend";
import { readArray, readNumber, readObject, readString } from "../json/read-json";
import type { ProjectPlace } from "./catalog";

/** A place in the using service that shows the use, with what it matches in the index now. */
export interface LinkPlace {
  readonly path: string;
  readonly files: number | null;
}

export interface ProjectLink {
  readonly fromProject: string;
  readonly fromService: string;
  readonly toProject: string;
  readonly toService: string;
  /** How it is used: a library, an HTTP API, a queue, or another way. */
  readonly kind: string;
  readonly note: string;
  readonly places: readonly LinkPlace[];
}

export interface ProjectLinks {
  /** From the project's services to services of this or another project. */
  readonly uses: readonly ProjectLink[];
  /** From services of other projects to the project's services. */
  readonly usedBy: readonly ProjectLink[];
}

function parseLink(value: unknown): ProjectLink {
  const link = readObject(value, "a link between services");
  return {
    fromProject: readString(link["from_project"]) ?? "",
    fromService: readString(link["from_service"]) ?? "",
    toProject: readString(link["to_project"]) ?? "",
    toService: readString(link["to_service"]) ?? "",
    kind: readString(link["kind"]) ?? "",
    note: readString(link["note"]) ?? "",
    places: readArray(link["paths"]).map((item) => {
      const place = readObject(item, "a place of a link");
      return { path: readString(place["path"]) ?? "", files: readNumber(place["files"]) };
    }),
  };
}

export function parseProjectLinks(value: unknown): ProjectLinks {
  const detail = readObject(value, "a project with its links");
  return {
    uses: readArray(detail["uses"]).map(parseLink),
    usedBy: readArray(detail["used_by"]).map(parseLink),
  };
}

/** One end of a link as it is named inside `project`: the service alone, or `project/service`. */
export function endLabel(project: string, end: { readonly project: string; readonly service: string }): string {
  return end.project === project ? end.service : `${end.project}/${end.service}`;
}

export async function readProjectLinks(backend: Backend, place: ProjectPlace, project: string): Promise<ProjectLinks> {
  const asked = { workspace: place.workspace, project };
  return parseProjectLinks(await backend.invoke("api_project", { id: place.id, place: asked }));
}
