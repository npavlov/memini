// Words and colours for the freshness the status reports. The definitions are the server's
// (docs/mcp/status.md); this module only names them for a reader.

import type { Tone } from "../components";
import type { ServiceStatus, StreamStatus, WorkspaceIndex } from "./status";

export interface Verdict {
  readonly tone: Tone;
  readonly label: string;
}

// Maps, not objects: the keys are looked up with the server's text, and an object would also
// answer for names such as "constructor".
const BEHIND_REASONS: ReadonlyMap<string, string> = new Map([
  ["never_checked", "never checked"],
  ["lag", "lagging"],
  ["not_indexed", "records not indexed"],
  ["index_stale", "index is stale"],
]);

/** A reason a stream is behind, in words; a reason this app does not know is shown as it is. */
export function behindReason(reason: string): string {
  return BEHIND_REASONS.get(reason) ?? reason;
}

export function streamVerdict(stream: StreamStatus): Verdict {
  if (stream.missing) {
    return { tone: "warn", label: "missing at the source" };
  }
  if (stream.current === false || stream.behind.length > 0) {
    const reasons = stream.behind.map(behindReason).join(", ");
    return { tone: "bad", label: reasons === "" ? "behind" : `behind: ${reasons}` };
  }
  if (stream.current === true) {
    return { tone: "good", label: "current" };
  }
  return { tone: "idle", label: "on demand" };
}

export function runTone(status: string): Tone {
  switch (status) {
    case "succeeded":
      return "good";
    case "failed":
      return "bad";
    case "partial":
    case "running":
      return "warn";
    default:
      return "idle";
  }
}

const INDEX_STATES: ReadonlyMap<string, Verdict> = new Map<string, Verdict>([
  ["ok", { tone: "good", label: "index ok" }],
  ["behind", { tone: "warn", label: "records not indexed" }],
  ["stale", { tone: "bad", label: "index stale: rebuild needed" }],
  ["none", { tone: "idle", label: "nothing indexed yet" }],
  ["off", { tone: "idle", label: "no embedding provider" }],
  ["unknown", { tone: "warn", label: "index unknown" }],
]);

export function indexVerdict(state: string): Verdict {
  return INDEX_STATES.get(state) ?? { tone: "info", label: state };
}

export function workspaceIndexVerdict(index: WorkspaceIndex): Verdict {
  if (index.behind && index.state === "ok") {
    return { tone: "warn", label: "records not indexed" };
  }
  return indexVerdict(index.state);
}

export interface Totals {
  readonly workspaces: number;
  readonly streams: number;
  readonly behind: number;
  readonly failing: number;
}

/** Counts over every workspace the token may read. */
export function totals(status: ServiceStatus): Totals {
  const streams = status.workspaces.flatMap((workspace) => workspace.streams);
  return {
    workspaces: status.workspaces.length,
    streams: streams.length,
    behind: streams.filter((stream) => streamVerdict(stream).tone === "bad").length,
    failing: streams.filter((stream) => stream.failures > 0).length,
  };
}
