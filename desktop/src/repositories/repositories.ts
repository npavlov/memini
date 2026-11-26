// Repositories without a terminal (scenario S17): what the launcher says of the folders the server
// may read and of its git access, what the API says of a workspace's git sources, and the checks
// of what the user types. Rust ran the launcher and called the API; this module reads the answers.

import { AppError } from "../ipc/app-error";
import { readArray, readBoolean, readNumber, readObject, readString, readStrings } from "../json/read-json";
import { displayText } from "../text/display-text";
import type { Tone } from "../components";

/** A folder of this computer the server reads. */
export interface LocalRepo {
  readonly name: string;
  /** The folder on this computer. */
  readonly path: string;
  /** Where the server sees it: the location of a git source. */
  readonly location: string;
  /** The branch checked out; null for a detached HEAD. */
  readonly branch: string | null;
  readonly present: boolean;
}

export interface ReposAnswer {
  readonly ok: boolean;
  readonly error: string | null;
  readonly repository: LocalRepo | null;
  readonly warning: string | null;
  readonly repositories: readonly LocalRepo[];
}

export interface KeyPrint {
  readonly kind: string;
  readonly fingerprint: string;
}

export interface HostKeys {
  readonly host: string;
  readonly keys: readonly KeyPrint[];
}

/** What identifies the server's git access. It holds no secret. */
export interface GitAccess {
  readonly ok: boolean;
  readonly error: string | null;
  readonly ssh: { readonly key: KeyPrint | null; readonly hosts: readonly HostKeys[] } | null;
  readonly https: { readonly hosts: readonly string[]; readonly username: string } | null;
}

/** A git source of a workspace. */
export interface GitSource {
  readonly name: string;
  readonly location: string;
  readonly refs: readonly string[];
  readonly scheduleSeconds: number | null;
  /** The globs of files it leaves out, and those it keeps (none: every file). */
  readonly exclude: readonly string[];
  readonly include: readonly string[];
}

/** The state of one tracked ref of a source. */
export interface RefState {
  readonly ref: string;
  readonly tone: Tone;
  readonly label: string;
  /** The reason of a failed run or attempt, as the server gave it. */
  readonly detail: string | null;
  /** A sync of the ref runs now. */
  readonly running: boolean;
  /** The newest run: the one that runs, or the last one. */
  readonly runId: string | null;
  /** When the newest run finished, as the server wrote it. */
  readonly finishedAt: string | null;
  /** What the index holds of the ref now, whatever the runs did; null from an older server. */
  readonly index: IndexState | null;
}

/** What the index holds of one ref (the stream's `index`). */
export interface IndexState {
  readonly files: number;
  readonly chunks: number;
  /** The commit the index holds the ref at; null before the first complete sync. */
  readonly commit: string | null;
  readonly committedAt: string | null;
}

function parseIndexState(value: unknown): IndexState | null {
  if (value === null || value === undefined) {
    return null;
  }
  const index = readObject(value, "what the index holds of a stream");
  const commit = readString(index["commit"]);
  return {
    files: readNumber(index["records"]) ?? 0,
    chunks: readNumber(index["chunks"]) ?? 0,
    commit: commit !== null && /^[0-9a-f]{7,64}$/u.test(commit) ? commit : null,
    committedAt: readString(index["committed_at"]),
  };
}

/** Where the secret of a git access comes from; the secret itself never reaches the front end. */
export type SecretSource = { readonly from: "clipboard" } | { readonly from: "file"; readonly path: string };

const HANDLE = /^[a-z0-9][a-z0-9_-]{0,62}$/u;
const HOST = /^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/u;

function text(value: unknown, limit = 400): string {
  return displayText(readString(value), limit);
}

function optionalText(value: unknown, limit = 400): string | null {
  const found = readString(value);
  return found === null ? null : displayText(found, limit);
}

function parseRepo(value: unknown): LocalRepo {
  const repo = readObject(value, "a folder of the server");
  return {
    name: text(repo["name"], 80),
    path: text(repo["path"], 1000),
    location: text(repo["location"], 200),
    branch: optionalText(repo["branch"], 200),
    present: readBoolean(repo["present"]) ?? false,
  };
}

export function parseReposAnswer(value: unknown): ReposAnswer {
  const answer = readObject(value, "the launcher's answer about folders");
  const repository = answer["repository"];
  return {
    ok: readBoolean(answer["ok"]) ?? false,
    error: optionalText(answer["error"]),
    repository: repository === null || repository === undefined ? null : parseRepo(repository),
    warning: optionalText(answer["warning"]),
    repositories: readArray(answer["repositories"]).map(parseRepo),
  };
}

function parsePrint(value: unknown): KeyPrint {
  const print = readObject(value, "a key fingerprint");
  return { kind: text(print["type"], 40), fingerprint: text(print["fingerprint"], 200) };
}

export function parseGitAccess(value: unknown): GitAccess {
  const answer = readObject(value, "the launcher's answer about git access");
  const ssh = answer["ssh"];
  const https = answer["https"];
  const sshAccess =
    ssh === null || ssh === undefined ? null : readObject(ssh, "the server's ssh access");
  const httpsAccess =
    https === null || https === undefined ? null : readObject(https, "the server's https access");
  return {
    ok: readBoolean(answer["ok"]) ?? false,
    error: optionalText(answer["error"]),
    ssh:
      sshAccess === null
        ? null
        : {
            key:
              sshAccess["key"] === null || sshAccess["key"] === undefined
                ? null
                : parsePrint(sshAccess["key"]),
            hosts: readArray(sshAccess["hosts"]).map((item) => {
              const host = readObject(item, "a host of the ssh access");
              return { host: text(host["host"], 253), keys: readArray(host["keys"]).map(parsePrint) };
            }),
          },
    https:
      httpsAccess === null
        ? null
        : {
            hosts: readStrings(httpsAccess["hosts"]).map((host) => displayText(host, 253)),
            username: text(httpsAccess["username"], 200),
          },
  };
}

/** The sources of kind repository of `GET /v1/workspaces/{workspace}/sources`; other kinds are left out. */
export function parseGitSources(value: unknown): readonly GitSource[] {
  const list = readObject(value, "the list of sources");
  return readArray(list["items"])
    .map((item) => readObject(item, "a source"))
    .filter((source) => readString(source["kind"]) === "repository")
    .map((source) => ({
      name: text(source["name"], 80),
      location: text(source["location"], 1000),
      refs: readStrings(source["refs"]).map((ref) => displayText(ref, 200)),
      scheduleSeconds: readNumber(source["schedule_seconds"]),
      exclude: readStrings(source["exclude_paths"]).map((glob) => displayText(glob, 400)),
      include: readStrings(source["include_paths"]).map((glob) => displayText(glob, 400)),
    }));
}

/** What each tracked ref of a source is doing, from its streams. */
export function parseRefStates(value: unknown): readonly RefState[] {
  const list = readObject(value, "the streams of a source");
  return readArray(list["items"]).map((item) => {
    const stream = readObject(item, "a stream");
    const ref = text(stream["key"], 200);
    const index = parseIndexState(stream["index"]);
    const pending = stream["pending_sync"];
    const last = stream["last_run"];
    const run = last === null || last === undefined ? null : readObject(last, "the newest run");
    const runId = run === null ? null : readString(run["id"]);
    const finishedAt = run === null ? null : readString(run["finished_at"]);
    if (pending !== null && pending !== undefined) {
      const job = readObject(pending, "a pending sync");
      const running = readString(job["status"]) === "running";
      return {
        ref,
        tone: "info",
        label: running ? "syncing" : "waiting to sync",
        detail: optionalText(job["last_error"]),
        running,
        runId: running && run !== null && readString(run["status"]) === "running" ? runId : null,
        finishedAt: null,
        index,
      };
    }
    if (run === null) {
      return { ref, tone: "idle", label: "not synced yet", detail: null, running: false, runId: null, finishedAt: null, index };
    }
    const status = readString(run["status"]) ?? "";
    const tone: Tone = status === "succeeded" ? "good" : status === "failed" ? "bad" : "warn";
    const label = status === "succeeded" ? "synced" : displayText(status, 40);
    return { ref, tone, label, detail: optionalText(run["error"]), running: false, runId, finishedAt, index };
  });
}

/** The error of a launcher answer that says it failed, with the launcher's own reason. */
export function launcherRefusal(error: string | null, fallback: string): AppError {
  return new AppError({ kind: "launcher_failed", message: error ?? fallback });
}

/** An input the user can correct, as an error to show. */
export function inputProblem(message: string): AppError {
  return new AppError({ kind: "invalid", message });
}

export function isHandle(name: string): boolean {
  return HANDLE.test(name);
}

/** A source name from a folder's or a repository's own name: lowercase, letters, digits and `-`. */
export function suggestName(from: string): string {
  const last = from.replace(/[\\/]+$/u, "").split(/[\\/:]/u).at(-1) ?? "";
  const name = last
    .replace(/\.git$/u, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/gu, "-")
    .replace(/^[-_]+|[-_]+$/gu, "")
    .slice(0, 63);
  return name === "" ? "repository" : name;
}

/** The hosts the user typed, separated by commas or spaces. */
export function splitHosts(typed: string): readonly string[] {
  return typed
    .split(/[\s,]+/u)
    .map((host) => host.trim().toLowerCase())
    .filter((host) => host !== "");
}

export function hostsProblem(typed: string): string | null {
  const hosts = splitHosts(typed);
  if (hosts.length === 0) {
    return "Name the host of the git server, such as git.example.org.";
  }
  const bad = hosts.find((host) => !HOST.test(host));
  return bad === undefined ? null : `“${bad}” is not a host name, such as git.example.org.`;
}

/** The host of a remote's address, for the hosts of its git access; null when it has none. */
export function hostOf(location: string): string | null {
  const scp = /^[^@/\s]+@([^:/\s]+):/u.exec(location);
  if (scp !== null) {
    return scp[1]?.toLowerCase() ?? null;
  }
  try {
    const url = new URL(location);
    return url.hostname === "" ? null : url.hostname.toLowerCase();
  } catch {
    return null;
  }
}

/** Whether a remote needs the server's ssh key: an ssh:// or scp-like address. */
export function isSshRemote(location: string): boolean {
  return location.startsWith("ssh://") || /^[^@/\s]+@[^:/\s]+:/u.test(location);
}

export interface RemoteForm {
  readonly location: string;
  readonly name: string;
  readonly ref: string;
}

/** Why the form of a remote repository cannot be sent yet; null when it can. */
export function remoteProblem(form: RemoteForm): string | null {
  const location = form.location.trim();
  if (location === "") {
    return "Enter the repository's address.";
  }
  if (/\s/u.test(location)) {
    return "An address has no spaces.";
  }
  if (!location.startsWith("https://") && !isSshRemote(location)) {
    return "Use an https:// address, an ssh:// one, or git@host:path.";
  }
  return sourceProblem(form.name, form.ref);
}

/** Why a source's name or ref cannot be used; null when both can. */
export function sourceProblem(name: string, ref: string): string | null {
  if (!isHandle(name)) {
    return "The name is lowercase letters, digits, '-' or '_', and starts with a letter or digit.";
  }
  if (ref.trim() === "" || /\s/u.test(ref.trim())) {
    return "Name the branch to follow, such as main.";
  }
  return null;
}

/**
 * Whether Docker Desktop on macOS may not show this folder to the server: it shares /Users,
 * /Volumes, /private, /tmp and /var/folders by default, and a user can take any of them out. A
 * folder outside the first two, and one on another disk, is worth a word before it is added.
 */
export function mayNeedFileSharing(os: string, path: string): boolean {
  return os === "macos" && !path.trim().startsWith("/Users/");
}

export const SCHEDULES: readonly { readonly value: string; readonly label: string }[] = [
  { value: "900", label: "Every 15 minutes" },
  { value: "3600", label: "Every hour" },
  { value: "86400", label: "Every day" },
  { value: "", label: "Only when I sync it" },
];

export function readSchedule(value: string): number | null {
  const seconds = Number.parseInt(value, 10);
  return Number.isFinite(seconds) ? seconds : null;
}

function samePlace(one: string, other: string): boolean {
  return one.replace(/\/+$/u, "") === other.replace(/\/+$/u, "");
}

/**
 * The folder of this computer a code base reads: the one the server sees at the source's
 * location. A mounted folder is the server's, not a workspace's, so it is shown with the code
 * base that uses it (memini-qpk.34).
 */
export function folderOf(folders: readonly LocalRepo[], location: string): LocalRepo | null {
  return folders.find((folder) => samePlace(folder.location, location)) ?? null;
}

/** The folders no source of any workspace reads: those the Server screen lists. */
export function unusedFolders(folders: readonly LocalRepo[], locations: readonly string[]): readonly LocalRepo[] {
  return folders.filter((folder) => !locations.some((location) => samePlace(folder.location, location)));
}
