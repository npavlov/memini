// The code bases of a workspace as the Codebases tab shows them (scenario S18): what each one
// indexes, how far its sync is, and the task an agent needs to map it to the business. Pure: the
// screen asks Rust for the data and this module turns it into words and globs.

import { readArray, readNumber, readObject, readString, readStrings } from "../json/read-json";
import { displayText } from "../text/display-text";
import { formatClock, formatDay } from "../text/format-time";
import type { InstantFormat } from "../text/format-time";
import type { RefState } from "./repositories";

// --- Scope -------------------------------------------------------------------------------------

export interface ScopePreset {
  readonly id: "tests" | "generated" | "data" | "assets";
  readonly label: string;
  readonly hint: string;
  /** Path globs of the server (`**` any folders, `*` within a name), for the common languages. */
  readonly globs: readonly string[];
}

/**
 * What is left out by default: what answers no question about how the business is served. Each
 * preset knows the common languages, so a repository of any of them, or of several, is covered;
 * a glob of a language the repository does not use matches nothing.
 */
export const SCOPE_PRESETS: readonly ScopePreset[] = [
  {
    id: "tests",
    label: "Tests",
    hint: "Unit and end-to-end tests of the common languages.",
    globs: [
      "**/*_test.go",
      "**/test_*.py",
      "**/*_test.py",
      "**/tests/**",
      "**/test/**",
      "**/__tests__/**",
      "**/*.test.*",
      "**/*.spec.*",
      "**/spec/**",
      "**/src/test/**",
      "**/*Test.java",
      "**/*Test.kt",
      "**/*Tests.cs",
      "**/*.Tests/**",
      "**/*Tests.swift",
      "**/*Tests/**",
      "**/e2e/**",
      "**/testdata/**",
      "**/fixtures/**",
      "**/__snapshots__/**",
      "**/*.snap",
    ],
  },
  {
    id: "generated",
    label: "Generated code and build output",
    hint: "Code a tool wrote, and what a build or a package manager put in the tree.",
    globs: [
      "**/*.pb.go",
      "**/*_pb2.py",
      "**/*_pb2_grpc.py",
      "**/*.g.dart",
      "**/*.freezed.dart",
      "**/*.Designer.cs",
      "**/*.generated.*",
      "**/*.gen.*",
      "**/*_gen.go",
      "**/generated/**",
      "**/vendor/**",
      "**/third_party/**",
      "**/dist/**",
      "**/build/**",
      "**/target/**",
      "**/bin/**",
      "**/obj/**",
      "**/Pods/**",
      "**/.next/**",
      "**/coverage/**",
    ],
  },
  {
    id: "data",
    label: "Data files",
    hint: "JSON, CSV, YAML fixtures and dumps: values, not behaviour.",
    globs: ["**/*.json", "**/*.ndjson", "**/*.csv", "**/*.tsv", "**/*.xml", "**/*.sql.gz", "**/*.dump"],
  },
  {
    id: "assets",
    label: "Styles and assets",
    hint: "Stylesheets, images, fonts and media.",
    globs: [
      "**/assets/**",
      "**/static/**",
      "**/public/**",
      "**/*.css",
      "**/*.scss",
      "**/*.less",
      "**/*.svg",
      "**/*.png",
      "**/*.jpg",
      "**/*.jpeg",
      "**/*.gif",
      "**/*.ico",
      "**/*.woff",
      "**/*.woff2",
      "**/*.ttf",
      "**/*.mp4",
      "**/*.pdf",
    ],
  },
];

export type PresetId = ScopePreset["id"];

export const DEFAULT_PRESETS: ReadonlySet<PresetId> = new Set(SCOPE_PRESETS.map((preset) => preset.id));

/** The exclude list of the chosen presets and the user's own globs, each glob once. */
export function excludeOf(presets: ReadonlySet<PresetId>, custom: readonly string[]): readonly string[] {
  const globs = SCOPE_PRESETS.filter((preset) => presets.has(preset.id)).flatMap((preset) => preset.globs);
  return [...new Set([...globs, ...custom.map((glob) => glob.trim()).filter((glob) => glob !== "")])];
}

/** Which presets an exclude list holds whole, and the globs that belong to none. */
export function scopeOf(exclude: readonly string[]): {
  readonly presets: ReadonlySet<PresetId>;
  readonly custom: readonly string[];
} {
  const held = new Set(exclude);
  const presets = new Set(
    SCOPE_PRESETS.filter((preset) => preset.globs.every((glob) => held.has(glob))).map((preset) => preset.id),
  );
  const known = new Set(
    SCOPE_PRESETS.filter((preset) => presets.has(preset.id)).flatMap((preset) => preset.globs),
  );
  return { presets, custom: exclude.filter((glob) => !known.has(glob)) };
}

/** The user's globs, one per line. */
export function splitGlobs(typed: string): readonly string[] {
  return typed
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

// --- Preview -----------------------------------------------------------------------------------

export interface PreviewGroup {
  readonly key: string;
  readonly tracked: number;
  readonly indexed: number;
}

export interface Preview {
  readonly tracked: number;
  readonly indexed: number;
  readonly byExtension: readonly PreviewGroup[];
  readonly byFolder: readonly PreviewGroup[];
}

function parseGroup(value: unknown): PreviewGroup {
  const group = readObject(value, "a group of the preview");
  return {
    key: displayText(readString(group["key"]), 200),
    tracked: readNumber(group["tracked"]) ?? 0,
    indexed: readNumber(group["indexed"]) ?? 0,
  };
}

export function parsePreview(value: unknown): Preview {
  const preview = readObject(value, "the preview of a code base");
  return {
    tracked: readNumber(preview["tracked"]) ?? 0,
    indexed: readNumber(preview["indexed"]) ?? 0,
    byExtension: readArray(preview["by_extension"]).map(parseGroup),
    byFolder: readArray(preview["by_folder"]).map(parseGroup),
  };
}

// --- Progress ----------------------------------------------------------------------------------

export interface SyncProgress {
  readonly total: number;
  readonly processed: number;
  readonly committed: number;
  /** When writing started, in milliseconds; null when the server did not say. */
  readonly startedAt: number | null;
}

/** The progress of a running sync, from its run (`GET /v1/runs/{id}`); null before it says. */
export function parseRunProgress(value: unknown): SyncProgress | null {
  const run = readObject(value, "a run");
  const report = run["report"];
  if (report === null || report === undefined) {
    return null;
  }
  const details = readObject(report, "a run report")["details"];
  if (details === null || details === undefined) {
    return null;
  }
  const progress = readObject(details, "the details of a run")["progress"];
  if (progress === null || progress === undefined) {
    return null;
  }
  const found = readObject(progress, "the progress of a sync");
  const started = Date.parse(readString(found["started_at"]) ?? "");
  return {
    total: readNumber(found["files_total"]) ?? 0,
    processed: readNumber(found["files_processed"]) ?? 0,
    committed: readNumber(found["files_committed"]) ?? 0,
    startedAt: Number.isNaN(started) ? null : started,
  };
}

/** Seconds left at the rate so far; null until the rate says something. */
export function secondsLeft(progress: SyncProgress, now: number): number | null {
  if (progress.startedAt === null || progress.processed < 1 || progress.processed >= progress.total) {
    return null;
  }
  const elapsed = (now - progress.startedAt) / 1000;
  if (elapsed < 15) {
    return null;
  }
  return ((progress.total - progress.processed) * elapsed) / progress.processed;
}

const NUMBER = new Intl.NumberFormat("en");

function about(seconds: number): string {
  if (seconds < 90) {
    return "about a minute left";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) {
    return `about ${String(minutes)} minutes left`;
  }
  const hours = Math.round(minutes / 6) / 10;
  return `about ${String(hours)} hours left`;
}

/** "312 of 1,472 files · about 25 minutes left". */
export function describeProgress(progress: SyncProgress, now: number): string {
  if (progress.total === 0) {
    return "Looking at what changed…";
  }
  const files = `${NUMBER.format(progress.processed)} of ${NUMBER.format(progress.total)} files`;
  const left = secondsLeft(progress, now);
  return left === null ? files : `${files} · ${about(left)}`;
}

// --- What a run did, and what the index holds ---------------------------------------------------

/** Explained once, beside the index state of a card. */
export const SYNC_EXPLAINED =
  "The first sync lists every file of the branch (a full listing). Later ones read only what changed since the indexed commit (a diff). Text that did not change keeps its vectors, so only new or changed text is embedded again.";

/** How a sync found its work: by listing the whole tree, or by a diff from the indexed commit. */
export type SyncMode = "full" | "diff";

export interface RunCheck {
  /** null when the report names neither step: a run that failed before it planned. */
  readonly mode: SyncMode | null;
  readonly added: number;
  readonly changed: number;
  readonly deleted: number;
  readonly unchanged: number;
  /** How far it is, while it runs; null before it says, and for a diff that writes nothing. */
  readonly progress: SyncProgress | null;
}

function count(counts: Record<string, unknown>, name: string): number {
  return readNumber(counts[name]) ?? 0;
}

/**
 * What a git sync did or is doing, from its run (`GET /v1/runs/{id}`): the step that planned it
 * (`list` for a full listing, `diff` for a diff; a diff that falls back to listing has both, and
 * is a full listing) and the counts of files.
 */
export function parseRunCheck(value: unknown): RunCheck {
  const run = readObject(value, "a run");
  const report = run["report"];
  const found = report === null || report === undefined ? {} : readObject(report, "a run report");
  const steps = readArray(found["steps"]).map((step) => readString(readObject(step, "a step")["name"]));
  const counts = found["counts"] === null || found["counts"] === undefined ? {} : readObject(found["counts"], "counts");
  const mode: SyncMode | null = steps.includes("list") ? "full" : steps.includes("diff") ? "diff" : null;
  return {
    mode,
    added: count(counts, "files_added"),
    changed: count(counts, "files_changed"),
    deleted: count(counts, "files_deleted"),
    unchanged: count(counts, "files_unchanged"),
    progress: parseRunProgress(value),
  };
}

/** "no changes", "3 changed, 1 deleted", or for a full listing "full listing: 1,472 files written". */
export function describeCheck(check: RunCheck): string {
  if (check.mode === "full") {
    const written = check.added + check.changed;
    const kept = check.unchanged > 0 ? `, ${NUMBER.format(check.unchanged)} unchanged` : "";
    return `full listing: ${NUMBER.format(written)} ${written === 1 ? "file" : "files"} written${kept}`;
  }
  const parts = [
    check.added > 0 ? `${NUMBER.format(check.added)} added` : "",
    check.changed > 0 ? `${NUMBER.format(check.changed)} changed` : "",
    check.deleted > 0 ? `${NUMBER.format(check.deleted)} deleted` : "",
  ].filter((part) => part !== "");
  return parts.length === 0 ? "no changes" : parts.join(", ");
}

// --- Sync now ----------------------------------------------------------------------------------

/** "main" for refs/heads/main, "v1.2" for refs/tags/v1.2. */
export function shortRef(ref: string): string {
  return ref.replace(/^refs\/(heads|tags)\//u, "");
}

/** Said beside Sync now for a folder of this computer: the server reads its branch, not the remote. */
export const LOCAL_FOLDER_SYNC =
  "The server reads the branch of this folder, not its remote: new commits appear here after you pull them into the working copy (git pull).";

/** What the syncs of a code base do now: one runs, one waits in the queue, or none is under way. */
export function syncPhase(refs: readonly RefState[]): "running" | "queued" | null {
  if (refs.some((ref) => ref.running)) {
    return "running";
  }
  return refs.some((ref) => ref.queued) ? "queued" : null;
}

function unchanged(check: RunCheck | undefined): boolean {
  return check !== undefined && check.mode === "diff" && describeCheck(check) === "no changes";
}

/** "; main is still at 0327a55, committed 1 Nov"; empty when the index holds no complete commit. */
function whereNow(ref: RefState, check: RunCheck | undefined, nowMs: number, format: InstantFormat): string {
  const commit = ref.index?.commit ?? null;
  if (commit === null) {
    return "";
  }
  const committedAt = ref.index?.committedAt ?? null;
  const day = committedAt === null ? "" : `, committed ${formatDay(committedAt, nowMs, format)}`;
  const at = unchanged(check) ? "is still at" : "is at";
  return `; ${shortRef(ref.ref)} ${at} ${commit.slice(0, 7)}${day}`;
}

/**
 * How the newest sync of a ref ended, in one line: "Checked 12:26 — no changes; main is still at
 * 0327a55, committed 1 Nov", "Checked 12:26 — 3 changed, 1 deleted; main is at 9f8e7d6, …", or
 * "Checked 12:26 — failed: why". Null while a sync waits or runs, and before the first one.
 */
export function describeOutcome(
  ref: RefState,
  check: RunCheck | undefined,
  nowMs: number,
  format: InstantFormat = {},
): string | null {
  if (ref.running || ref.queued || ref.finishedAt === null) {
    return null;
  }
  const at = `Checked ${formatClock(ref.finishedAt, nowMs, format)}`;
  if (ref.detail !== null) {
    return `${at} — ${ref.label}: ${ref.detail}`;
  }
  const what = check === undefined ? ref.label : describeCheck(check);
  return `${at} — ${what}${whereNow(ref, check, nowMs, format)}`;
}

/**
 * The refs whose sync, asked for with Sync now, has ended: none while one still waits or runs;
 * then every ref whose newest run is not the one it had when the sync was asked for, or, once a
 * sync was seen under way, every ref.
 */
export function endedSyncs(
  refs: readonly RefState[],
  before: ReadonlyMap<string, string | null>,
  seenUnderWay: boolean,
): readonly RefState[] {
  if (syncPhase(refs) !== null) {
    return [];
  }
  return refs.filter((ref) => seenUnderWay || ref.runId !== (before.get(ref.ref) ?? null));
}

/** The label of a running sync's bar: which kind of sync it is. */
export function progressLabel(mode: SyncMode | null): string {
  if (mode === "full") {
    return "Full listing: every file of the branch";
  }
  return mode === "diff" ? "Diff: what changed since the indexed commit" : "Fetching the repository";
}

/** "1,472 files · 9,310 chunks". */
export function describeHeld(files: number, chunks: number): string {
  return `${NUMBER.format(files)} ${files === 1 ? "file" : "files"} · ${NUMBER.format(chunks)} ${chunks === 1 ? "chunk" : "chunks"}`;
}

// --- How much of a code base the project map names -------------------------------------------

export interface ProjectMapping {
  readonly project: string;
  readonly services: readonly string[];
  readonly domains: readonly string[];
  readonly files: number;
  readonly updatedAt: string | null;
  /** The token that wrote the project last; null for a change without one. */
  readonly updatedToken: string | null;
}

export interface Coverage {
  readonly files: number;
  readonly covered: number;
  /** 0 to 1; null for a code base without files. */
  readonly share: number | null;
  readonly projects: readonly ProjectMapping[];
  readonly uncovered: readonly { readonly folder: string; readonly files: number }[];
}

/** `GET …/sources/{source}/coverage`. Names are the server's and shown as text. */
export function parseCoverage(value: unknown): Coverage {
  const found = readObject(value, "the coverage of a code base");
  return {
    files: readNumber(found["files"]) ?? 0,
    covered: readNumber(found["covered"]) ?? 0,
    share: readNumber(found["covered_share"]),
    projects: readArray(found["projects"]).map((item) => {
      const project = readObject(item, "a project of the coverage");
      return {
        project: displayText(readString(project["project"]), 255),
        services: readStrings(project["services"]).map((name) => displayText(name, 255)),
        domains: readStrings(project["domains"]).map((name) => displayText(name, 255)),
        files: readNumber(project["files"]) ?? 0,
        updatedAt: readString(project["updated_at"]),
        updatedToken: readString(project["updated_token"]),
      };
    }),
    uncovered: readArray(found["uncovered_folders"]).map((item) => {
      const folder = readObject(item, "a folder of the coverage");
      return { folder: displayText(readString(folder["folder"]), 255), files: readNumber(folder["files"]) ?? 0 };
    }),
  };
}

/** "Mapped: 80% of 1,472 files in a domain" or "Not mapped yet". */
export function describeCoverage(coverage: Coverage): string {
  if (coverage.projects.length === 0) {
    return "Not mapped to the business yet";
  }
  if (coverage.share === null) {
    return "Mapped; no file is indexed yet";
  }
  const percent = Math.round(coverage.share * 100);
  return `Mapped: ${String(percent)}% of ${NUMBER.format(coverage.files)} files are in a domain`;
}

/** The three steps of "Map the code bases to the business". */
export const MAPPING_STEPS: readonly string[] = [
  "Copy the task: it lists the code bases of this workspace and how to write the project map.",
  "Paste it to your agent that is connected to Memini (Claude Code, Cursor, Codex…). It reads the code and writes projects and domains with put_project.",
  "Check the result on the Project map screen and correct it there; this card then shows how much of the code base is mapped.",
];

// --- A task for an agent -----------------------------------------------------------------------

export interface TaskCodebase {
  readonly name: string;
  /** Where the server reads it; null where only its name is known, as on the project map. */
  readonly location: string | null;
}

/**
 * What an agent connected to Memini needs to propose the project map of a workspace (memini-qpk.24):
 * which code bases, what to read first, and how to write the map with `put_project`. The person
 * then reviews the map in the app.
 */
export function agentTask(workspace: string, codebases: readonly TaskCodebase[]): string {
  const listed = codebases
    .map((codebase) =>
      codebase.location === null ? `- ${codebase.name}` : `- ${codebase.name} (${codebase.location})`,
    )
    .join("\n");
  return [
    `Build the project map of the Memini workspace "${workspace}".`,
    "",
    "Code bases in it:",
    listed === "" ? "- (none yet: add one on the Codebases tab first)" : listed,
    "",
    "1. Call catalog_guide and follow it. Call get_catalog to see what is mapped already.",
    "2. Read what explains the business first: README and other Markdown files, docs folders, and",
    "   the comments at the top of packages, modules and main types (search_code, get_file).",
    "3. Propose projects, and in each the business domains and subdomains the code serves (for",
    "   example payments → refunds), in the words the team uses. For each one, name the services",
    "   (a repository, or a folder of one) and the path masks of the code that belongs to it.",
    "4. Write each project with put_project. Keep masks narrow; leave shared code unmapped rather",
    "   than guess.",
    "5. Report what you mapped, what you left out and why. A person reviews the map in the",
    "   Memini app (Project map) and corrects it there.",
  ].join("\n");
}
