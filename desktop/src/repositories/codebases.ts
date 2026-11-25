// The code bases of a workspace as the Codebases tab shows them (scenario S18): what each one
// indexes, how far its sync is, and the task an agent needs to map it to the business. Pure: the
// screen asks Rust for the data and this module turns it into words and globs.

import { readArray, readNumber, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";

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
