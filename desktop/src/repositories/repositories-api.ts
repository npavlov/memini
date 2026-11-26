// The Rust commands of scenario S17. The launcher commands work on the server of this computer;
// the API commands on the server of a connection, with its token, which only Rust holds.

import type { Backend } from "../ipc/backend";
import { parseWorkspaceNames } from "../tokens/tokens";
import { parsePreview, parseRunProgress } from "./codebases";
import type { Preview, SyncProgress } from "./codebases";
import { parseGitAccess, parseGitSources, parseRefStates, parseReposAnswer } from "./repositories";
import type { GitAccess, GitSource, RefState, ReposAnswer, SecretSource } from "./repositories";

export async function listFolders(backend: Backend): Promise<ReposAnswer> {
  return parseReposAnswer(await backend.invoke("launcher_repos", { request: { action: "list" } }));
}

/** Gives the server the folder at `path`; Rust checks it is a git repository first. */
export async function addFolder(backend: Backend, path: string): Promise<ReposAnswer> {
  return parseReposAnswer(
    await backend.invoke("launcher_repos", { request: { action: "add", path } }),
  );
}

export async function removeFolder(backend: Backend, name: string): Promise<ReposAnswer> {
  return parseReposAnswer(
    await backend.invoke("launcher_repos", { request: { action: "remove", name } }),
  );
}

export async function readGitAccess(backend: Backend): Promise<GitAccess> {
  return parseGitAccess(await backend.invoke("launcher_git", { request: { action: "status" } }));
}

/** Gives the server an ssh key, read by Rust from `source`, and the host keys of `hosts`. */
export async function setSshKey(
  backend: Backend,
  hosts: readonly string[],
  source: SecretSource,
): Promise<GitAccess> {
  return parseGitAccess(
    await backend.invoke("launcher_git", { request: { action: "ssh_key", hosts, source } }),
  );
}

/** Gives the server an https token, read by Rust from `source`, sent only to `hosts`. */
export async function setHttpsToken(
  backend: Backend,
  hosts: readonly string[],
  username: string,
  source: SecretSource,
): Promise<GitAccess> {
  const request = { action: "https_token", hosts, username: username.trim() || null, source };
  return parseGitAccess(await backend.invoke("launcher_git", { request }));
}

export async function removeGitAccess(backend: Backend, kind: "ssh" | "https"): Promise<GitAccess> {
  return parseGitAccess(await backend.invoke("launcher_git", { request: { action: "remove", kind } }));
}

export async function listWorkspaces(backend: Backend, id: string): Promise<readonly string[]> {
  return parseWorkspaceNames(await backend.invoke("api_workspaces", { id }));
}

export async function createWorkspace(backend: Backend, id: string, name: string): Promise<void> {
  await backend.invoke("api_workspace_create", { id, name });
}

export async function listGitSources(
  backend: Backend,
  id: string,
  workspace: string,
): Promise<readonly GitSource[]> {
  return parseGitSources(await backend.invoke("api_sources", { id, workspace }));
}

export interface NewSource {
  readonly workspace: string;
  readonly name: string;
  readonly location: string;
  readonly ref: string;
  readonly scheduleSeconds: number | null;
  /** Globs of files left out from the start (scenario S18). */
  readonly exclude: readonly string[];
}

/** Adds a git source and asks for its first sync at once. */
export async function addSource(backend: Backend, id: string, source: NewSource): Promise<void> {
  await backend.invoke("api_source_create", {
    id,
    source: {
      workspace: source.workspace,
      name: source.name,
      location: source.location.trim(),
      refs: [source.ref.trim()],
      schedule_seconds: source.scheduleSeconds,
      exclude_paths: source.exclude,
    },
  });
  await syncSource(backend, id, source.workspace, source.name);
}

export async function readRefStates(
  backend: Backend,
  id: string,
  workspace: string,
  source: string,
): Promise<readonly RefState[]> {
  return parseRefStates(await backend.invoke("api_source_streams", { id, source: { workspace, source } }));
}

export async function syncSource(
  backend: Backend,
  id: string,
  workspace: string,
  source: string,
): Promise<void> {
  await backend.invoke("api_source_sync", { id, source: { workspace, source } });
}

export async function deleteSource(
  backend: Backend,
  id: string,
  workspace: string,
  source: string,
): Promise<void> {
  await backend.invoke("api_source_delete", { id, source: { workspace, source } });
}

/** What a code base indexes and when it syncs; a field left out stays as it is. */
export interface SourceChanges {
  readonly exclude?: readonly string[];
  readonly include?: readonly string[];
  /** Off: only when asked; a number: every so many seconds. */
  readonly schedule?: "off" | number;
}

function changesBody(changes: SourceChanges): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (changes.exclude !== undefined) {
    body["exclude_paths"] = changes.exclude;
  }
  if (changes.include !== undefined) {
    body["include_paths"] = changes.include;
  }
  if (changes.schedule === "off") {
    body["schedule"] = { kind: "off" };
  } else if (changes.schedule !== undefined) {
    body["schedule"] = { kind: "every", seconds: changes.schedule };
  }
  return body;
}

/** Changes the scope or the schedule of a code base. A new scope lists the repository again. */
export async function updateSource(
  backend: Backend,
  id: string,
  workspace: string,
  source: string,
  changes: SourceChanges,
): Promise<void> {
  await backend.invoke("api_source_update", {
    id,
    source: { workspace, source },
    changes: changesBody(changes),
  });
}

/** What a sync would index with `rules`, or with the code base's own when none are given. */
export async function previewSource(
  backend: Backend,
  id: string,
  workspace: string,
  source: string,
  rules: SourceChanges,
): Promise<Preview> {
  return parsePreview(
    await backend.invoke("api_source_preview", { id, source: { workspace, source }, rules: changesBody(rules) }),
  );
}

/** How far a running sync is, from its run; null before it says. */
export async function readSyncProgress(backend: Backend, id: string, run: string): Promise<SyncProgress | null> {
  return parseRunProgress(await backend.invoke("api_run", { id, run }));
}
