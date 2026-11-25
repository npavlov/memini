// Why the server refused to save a project, in the user's terms. The server's codes are stable;
// its message is shown beside the app's words, as text. A refusal writes nothing.

import type { AppError } from "../ipc/app-error";
import { displayText } from "../text/display-text";
import type { Project } from "./catalog";

export interface Refusal {
  readonly title: string;
  /** What it means and what to do. */
  readonly text: string;
  /** The server's own words, when they add something. */
  readonly said: string | null;
}

const FIELDS: Readonly<Record<string, string>> = {
  name: "name",
  description: "description",
  repository: "repository",
  root: "directory",
  title: "title",
  uses: "links",
  code: "code",
  subdomains: "subdomains",
};

const PLACE = /^(services|domains)\[(\d+)\](?:\.([a-z_]+))?/u;

/**
 * A field of the project document, as the server names it (`services[1].root`), in words:
 * "the directory of service fees". The field as it came when it is not one of those.
 */
export function fieldWords(field: string, draft: Project): string {
  const match = PLACE.exec(field);
  if (match === null) {
    return field;
  }
  const [, list, position, part] = match;
  const at = Number(position);
  const named = list === "services" ? draft.services[at]?.name : draft.domains[at]?.name;
  const what = list === "services" ? "service" : "domain";
  const owner = named === undefined || named.trim() === "" ? `${what} ${String(at + 1)}` : `${what} ${named.trim()}`;
  const words = part === undefined ? undefined : FIELDS[part];
  return words === undefined ? owner : `the ${words} of ${owner}`;
}

const INVALID = /^The project document is not valid: ([^:]+): (.+?)\.?$/u;

function invalid(error: AppError, draft: Project): Refusal {
  const match = INVALID.exec(error.message);
  const field = match?.[1];
  const reason = match?.[2];
  if (field === undefined || reason === undefined) {
    return { title: "The server did not accept the project", text: "Correct it and save again.", said: error.message };
  }
  return {
    title: "The server did not accept the project",
    text: `Not valid: ${displayText(fieldWords(field, draft))}. Correct it and save again.`,
    said: reason,
  };
}

/**
 * A refusal of the save, by the server's code; null for anything else, which is shown as the
 * error it is. A version conflict is not described here: it has its own answer, a newer version
 * to load.
 */
export function describeRefusal(error: AppError | null, draft: Project): Refusal | null {
  switch (error?.code) {
    case "service_conflict":
      return {
        title: "That directory is already a service",
        text: "A directory of a repository is one service in the workspace, and a service of another project is that directory already. Choose another directory or repository, or name that project’s service in a domain instead.",
        said: null,
      };
    case "service_in_use":
      return {
        title: "Another project names a service you removed or renamed",
        text: "A domain or a link of another project names it, and a service that is renamed is a new one to the server. Change that project first, or keep the service and its name.",
        said: error.message,
      };
    case "invalid_project":
      return invalid(error, draft);
    case "not_found":
      return {
        title: "The server does not know something the project names",
        text: "A repository or a service the project names is not there. Choose a repository of this workspace, and name only services that exist.",
        said: error.message,
      };
    case "catalog_conflict":
      return {
        title: "The catalog changed while the project was saved",
        text: "Nothing was saved. Read the catalog again and save once more.",
        said: null,
      };
    default:
      return null;
  }
}

/** Whether creating a project was refused because one of that name exists. */
export function isNameTaken(error: AppError | null): boolean {
  return error?.code === "version_conflict";
}
