// Ollama on this computer, where the search model of a local server runs on macOS (memini-qpk.28).
// Docker on macOS has no GPU, so the model in the server's container embeds on the CPU of Docker's
// virtual machine: about 12.5 s a chunk, where Ollama on the Mac takes about 0.11 s. Rust asks
// Ollama at its fixed loopback address and runs Homebrew with fixed arguments; this module reads
// Rust's answers and turns them into words and decisions.

import type { Backend } from "../ipc/backend";
import { readArray, readBoolean, readNumber, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";

export interface PullProgress {
  readonly status: string;
  readonly completed: number;
  readonly total: number;
  readonly done: boolean;
  readonly error: string | null;
}

export interface InstallOutcome {
  readonly ok: boolean;
  readonly error: string | null;
  readonly tail: readonly string[];
}

export interface HostModel {
  /** macOS: the model belongs on this computer. Elsewhere nothing else is asked. */
  readonly supported: boolean;
  readonly found: boolean;
  readonly version: string | null;
  readonly model: string;
  readonly hasModel: boolean;
  /** Homebrew can install Ollama. */
  readonly brew: boolean;
  readonly pull: PullProgress | null;
  readonly installing: boolean;
  readonly install: InstallOutcome | null;
}

/** Where the search model runs, as the launcher keeps it. */
export type Runtime = "container" | "host";

/** Where Ollama is downloaded from, for a Mac without Homebrew. Shown as text to copy. */
export const OLLAMA_DOWNLOAD = "https://ollama.com/download";

/** How much faster Ollama on the Mac embeds than the container, measured 2026-11-25. */
export const SPEEDUP = "about a hundred times faster";

function shown(value: unknown): string | null {
  const text = readString(value);
  return text === null ? null : displayText(text);
}

export function parsePull(value: unknown): PullProgress | null {
  if (value === null || value === undefined) {
    return null;
  }
  const pull = readObject(value, "the download of the search model");
  return {
    status: shown(pull["status"]) ?? "",
    completed: readNumber(pull["completed"]) ?? 0,
    total: readNumber(pull["total"]) ?? 0,
    done: readBoolean(pull["done"]) ?? false,
    error: shown(pull["error"]),
  };
}

export function parseInstall(value: unknown): InstallOutcome | null {
  if (value === null || value === undefined) {
    return null;
  }
  const install = readObject(value, "the installation of Ollama");
  return {
    ok: readBoolean(install["ok"]) ?? false,
    error: shown(install["error"]),
    tail: readArray(install["tail"]).flatMap((line) => {
      const text = shown(line);
      return text === null ? [] : [text];
    }),
  };
}

export function parseHostModel(value: unknown): HostModel {
  const host = readObject(value, "Ollama on this computer");
  return {
    supported: readBoolean(host["supported"]) ?? false,
    found: readBoolean(host["found"]) ?? false,
    version: shown(host["version"]),
    model: shown(host["model"]) ?? "the search model",
    hasModel: readBoolean(host["has_model"]) ?? false,
    brew: readBoolean(host["brew"]) ?? false,
    pull: parsePull(host["pull"]),
    installing: readBoolean(host["installing"]) ?? false,
    install: parseInstall(host["install"]),
  };
}

const MB = 1_000_000;

/** How far a download is, in a short sentence. */
export function describePull(pull: PullProgress, model: string): string {
  if (pull.error !== null) {
    return pull.error;
  }
  if (pull.done) {
    return `${model} is on this Mac.`;
  }
  if (pull.total > 0) {
    const percent = Math.floor((pull.completed / pull.total) * 100);
    const done = Math.round(pull.completed / MB);
    const total = Math.round(pull.total / MB);
    return `Downloading ${model} to Ollama on this Mac: ${String(percent)}%, ${String(done)} of ${String(total)} MB.`;
  }
  return `Downloading ${model} to Ollama on this Mac…`;
}

/**
 * What the setup does about the search model:
 * - `container`: not a Mac; the model runs in the server's container, as before;
 * - `ready`: Ollama on this Mac has the model;
 * - `download`: Ollama on this Mac answers but has no model yet;
 * - `missing`: no Ollama answers; the user decides whether to install it.
 */
export type ModelPlan = "container" | "ready" | "download" | "missing";

export function modelPlan(host: HostModel): ModelPlan {
  if (!host.supported) {
    return "container";
  }
  if (!host.found) {
    return "missing";
  }
  return host.hasModel ? "ready" : "download";
}

export async function readHostModel(backend: Backend): Promise<HostModel> {
  return parseHostModel(await backend.invoke("host_model_status"));
}

/** Answers when the download ended; its progress is in {@link readHostModel} meanwhile. */
export async function pullHostModel(backend: Backend): Promise<PullProgress> {
  return parsePull(await backend.invoke("host_model_pull")) ?? {
    status: "",
    completed: 0,
    total: 0,
    done: true,
    error: "The app got no answer about the download.",
  };
}

export async function installOllama(backend: Backend): Promise<InstallOutcome> {
  return parseInstall(await backend.invoke("host_ollama_install")) ?? {
    ok: false,
    error: "The app got no answer about the installation.",
    tail: [],
  };
}
