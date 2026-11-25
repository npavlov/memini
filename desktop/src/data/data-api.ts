// The Data screen's commands of the Rust side. All of them read: Rust sends a GET to the route
// of a read tool, or to the routes of the status and of a workspace's sources.

import { AppError, toAppError } from "../ipc/app-error";
import type { Backend, CommandArguments } from "../ipc/backend";
import { readNumber, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";
import { readServiceStatus } from "../status/status";
import type { WorkspaceStatus } from "../status/status";
import { overviewOf, parseListedSources } from "./overview";
import type { ListedSource, WorkspaceOverview } from "./overview";
import { parseTools } from "./tool-schema";
import type { Tool } from "./tool-schema";

/** What a read tool answered, with the request that produced it. */
export interface ToolAnswer {
  /** The method and the path with its query, as Rust sent it. */
  readonly request: string;
  readonly status: number | null;
  /** The JSON of a successful answer, as the server sent it. */
  readonly body: unknown;
  /** The server's error, when it refused the call. */
  readonly error: AppError | null;
}

const WORKSPACE_NAME = /^[a-z0-9][a-z0-9_-]{0,62}$/u;
/** The workspaces whose sources are listed; more than these are shown from the status alone. */
const MAX_LISTED_WORKSPACES = 25;

export async function listTools(backend: Backend, id: string): Promise<readonly Tool[]> {
  return parseTools(await backend.invoke("api_tools", { id }));
}

export async function callTool(
  backend: Backend,
  id: string,
  call: CommandArguments,
): Promise<ToolAnswer> {
  const answer = readObject(await backend.invoke("api_tool_call", { id, call }), "a tool's answer");
  const error = answer["error"];
  return {
    request: displayText(readString(answer["request"]), 2000),
    status: readNumber(answer["status"]),
    body: answer["body"] ?? null,
    error: error === null || error === undefined ? null : toAppError(error),
  };
}

/** A workspace's sources, or null when they cannot be listed: the status still names them. */
async function listSources(
  backend: Backend,
  id: string,
  workspace: string,
): Promise<readonly ListedSource[] | null> {
  if (!WORKSPACE_NAME.test(workspace)) {
    return null;
  }
  try {
    return parseListedSources(await backend.invoke("api_sources", { id, workspace }));
  } catch (error) {
    if (error instanceof AppError) {
      return null;
    }
    throw error;
  }
}

async function overviewFor(
  backend: Backend,
  id: string,
  workspace: WorkspaceStatus,
): Promise<WorkspaceOverview> {
  return overviewOf(workspace, await listSources(backend, id, workspace.name));
}

/** What the base holds: every workspace the token may read, with its sources. */
export async function readOverview(
  backend: Backend,
  id: string,
): Promise<readonly WorkspaceOverview[]> {
  const status = await readServiceStatus(backend, id);
  const listed = status.workspaces.slice(0, MAX_LISTED_WORKSPACES);
  const rest = status.workspaces.slice(MAX_LISTED_WORKSPACES);
  const overviews = await Promise.all(listed.map((item) => overviewFor(backend, id, item)));
  return [...overviews, ...rest.map((item) => overviewOf(item, null))];
}
