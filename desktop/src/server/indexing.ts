// How much of this computer the search model uses while it embeds (scenario S18). The launcher
// keeps a mode in the managed server's Compose files; Rust runs it with a fixed argument vector
// for each mode, and this module reads its answer and turns it into words.

import type { Backend } from "../ipc/backend";
import { readBoolean, readNumber, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";

export const INDEXING_MODES = ["half", "background", "full"] as const;

export type IndexingMode = (typeof INDEXING_MODES)[number];

export interface IndexingState {
  readonly mode: IndexingMode;
  /** The cores the model may use; null for no limit. */
  readonly cpus: number | null;
  readonly cores: number;
}

export interface IndexingAnswer {
  readonly ok: boolean;
  readonly error: string | null;
  readonly indexing: IndexingState | null;
}

function isMode(value: string | null): value is IndexingMode {
  return INDEXING_MODES.some((mode) => mode === value);
}

export function parseIndexingAnswer(value: unknown): IndexingAnswer {
  const answer = readObject(value, "the launcher's indexing answer");
  const error = readString(answer["error"]);
  const found = answer["indexing"];
  let indexing: IndexingState | null = null;
  if (found !== null && found !== undefined) {
    const state = readObject(found, "the indexing state");
    const mode = readString(state["mode"]);
    indexing = {
      mode: isMode(mode) ? mode : "half",
      cpus: readNumber(state["cpus"]),
      cores: readNumber(state["cores"]) ?? 1,
    };
  }
  return {
    ok: readBoolean(answer["ok"]) ?? false,
    error: error === null ? null : displayText(error),
    indexing,
  };
}

export const MODE_LABELS: Readonly<Record<IndexingMode, string>> = {
  half: "Half the cores (recommended)",
  background: "In the background: one or two cores",
  full: "Every core: fastest, the computer is busy",
};

/** What the limit means, in a sentence. */
export function describeIndexing(state: IndexingState): string {
  if (state.cpus === null) {
    return `The search model may use all ${String(state.cores)} cores while it indexes. The first sync of a large repository is fastest, and the computer is slow meanwhile.`;
  }
  return `The search model uses at most ${String(state.cpus)} of ${String(state.cores)} cores while it indexes. The rest stay free for your work.`;
}

export async function readIndexing(backend: Backend): Promise<IndexingAnswer> {
  return parseIndexingAnswer(await backend.invoke("launcher_indexing"));
}

export async function setIndexing(backend: Backend, mode: IndexingMode): Promise<IndexingAnswer> {
  return parseIndexingAnswer(await backend.invoke("launcher_indexing_set", { mode }));
}
