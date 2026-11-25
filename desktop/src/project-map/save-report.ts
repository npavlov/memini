// What `put_project` answers after a save: the new version, and what the masks of the document
// match in the index now, so the project can be corrected.

import type { Tone } from "../components";
import type { AppError } from "../ipc/app-error";
import { readArray, readBoolean, readNumber, readObject, readString, readStrings } from "../json/read-json";
import { countNoun } from "../text/count-noun";

export interface EmptyMask {
  /** The domain, or `domain/subdomain`. */
  readonly domain: string;
  readonly service: string;
  readonly path: string;
}

export interface Overlap {
  /** The two domains that name the same files. */
  readonly domains: readonly string[];
  readonly source: string;
  readonly files: number | null;
}

export interface Coverage {
  readonly service: string;
  readonly files: number | null;
  /** Of them, files that no domain names. */
  readonly unassigned: number | null;
}

export interface SaveReport {
  readonly version: number | null;
  readonly created: boolean;
  /** False: the project already said what was saved, and its version stayed. */
  readonly changed: boolean;
  /** Secrets that were masked in titles and descriptions. */
  readonly redacted: number;
  readonly emptyMasks: readonly EmptyMask[];
  readonly overlaps: readonly Overlap[];
  readonly services: readonly Coverage[];
}

function parseEmptyMask(value: unknown): EmptyMask {
  const mask = readObject(value, "a mask of the project");
  return {
    domain: readString(mask["domain"]) ?? "",
    service: readString(mask["service"]) ?? "",
    path: readString(mask["path"]) ?? "",
  };
}

function parseOverlap(value: unknown): Overlap {
  const overlap = readObject(value, "two domains that share files");
  return {
    domains: readStrings(overlap["domains"]),
    source: readString(overlap["source"]) ?? "",
    files: readNumber(overlap["files"]),
  };
}

function parseCoverage(value: unknown): Coverage {
  const service = readObject(value, "a service of the project");
  return {
    service: readString(service["service"]) ?? "",
    files: readNumber(service["files"]),
    unassigned: readNumber(service["unassigned"]),
  };
}

function sumOf(value: unknown): number {
  const counts = Object.values(readObject(value ?? {}, "the secrets that were masked"));
  return counts.reduce<number>((sum, count) => sum + (readNumber(count) ?? 0), 0);
}

export function parseSaveReport(value: unknown): SaveReport {
  const answer = readObject(value, "the answer to saving a project");
  return {
    version: readNumber(answer["version"]),
    created: readBoolean(answer["created"]) ?? false,
    changed: readBoolean(answer["changed"]) ?? false,
    redacted: sumOf(answer["redacted"]),
    emptyMasks: readArray(answer["empty_masks"]).map(parseEmptyMask),
    overlaps: readArray(answer["overlaps"]).map(parseOverlap),
    services: readArray(answer["services"]).map(parseCoverage),
  };
}

export interface SaveSummary {
  readonly tone: Tone;
  readonly title: string;
  /** What to look at, one sentence each. */
  readonly lines: readonly string[];
}

function titleOf(report: SaveReport): string {
  const version = report.version === null ? "" : ` as version ${String(report.version)}`;
  if (!report.changed) {
    return `Nothing changed: the project already says this${version === "" ? "" : ` (version ${String(report.version)})`}.`;
  }
  return report.created ? `The project was created${version}.` : `Saved${version}.`;
}

function coverageLine(service: Coverage): string | null {
  if (service.files === null || service.unassigned === null || service.unassigned === 0) {
    return null;
  }
  return `${service.service}: ${String(service.unassigned)} of ${countNoun(service.files, "file")} are in no domain.`;
}

/** The answer to a save in words: what was written, and what the masks do not match as meant. */
export function describeSave(report: SaveReport): SaveSummary {
  const empty = report.emptyMasks.map(
    (mask) => `${mask.domain}: the mask ${mask.path} of ${mask.service} matches no file.`,
  );
  const overlaps = report.overlaps.map(
    (overlap) =>
      `${overlap.domains.join(" and ")} both name ${countNoun(overlap.files ?? 0, "file")} of ${overlap.source}.`,
  );
  const masked =
    report.redacted === 0
      ? []
      : [`${countNoun(report.redacted, "secret")} in titles and descriptions ${report.redacted === 1 ? "was" : "were"} masked before saving.`];
  const coverage = report.services.map(coverageLine).filter((line) => line !== null);
  return {
    tone: empty.length + overlaps.length > 0 ? "warn" : "good",
    title: titleOf(report),
    lines: [...empty, ...overlaps, ...masked, ...coverage],
  };
}

/**
 * Whether a save was refused because someone changed the project after it was read. The server
 * answers 409 for other conflicts too (a service still in use), so the code decides.
 */
export function isVersionConflict(error: AppError | null): boolean {
  return error?.code === "version_conflict";
}
