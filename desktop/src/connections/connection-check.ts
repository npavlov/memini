// What a check of a connection found: the server's version and readiness, which need no token,
// and whether the stored token may read.

import { readBoolean, readNumber, readObject, readString } from "../json/read-json";
import { countNoun } from "../text/count-noun";
import { displayText } from "../text/display-text";
import type { Tone } from "../components";

export const TOKEN_CHECKS = ["missing", "accepted", "refused", "no_read_scope", "unknown"] as const;

export type TokenCheck = (typeof TOKEN_CHECKS)[number];

export interface ConnectionCheck {
  readonly version: string;
  readonly ready: boolean | null;
  readonly token: TokenCheck;
  readonly workspaces: number | null;
  readonly detail: string | null;
}

export interface Finding {
  readonly tone: Tone;
  readonly text: string;
}

function readTokenCheck(value: unknown): TokenCheck {
  return TOKEN_CHECKS.find((check) => check === value) ?? "unknown";
}

export function parseConnectionCheck(value: unknown): ConnectionCheck {
  const check = readObject(value, "the result of a connection check");
  return {
    version: displayText(readString(check["version"]), 40),
    ready: readBoolean(check["ready"]),
    token: readTokenCheck(check["token"]),
    workspaces: readNumber(check["workspaces"]),
    detail: readString(check["detail"]),
  };
}

export function describeReadiness(check: ConnectionCheck): Finding {
  const version = check.version === "" ? "an unknown version" : `version ${check.version}`;
  if (check.ready === true) {
    return { tone: "good", text: `The server answers (${version}) and is ready.` };
  }
  if (check.ready === false) {
    return { tone: "warn", text: `The server answers (${version}) but is not ready yet.` };
  }
  return { tone: "info", text: `The server answers (${version}).` };
}

function describeAccepted(workspaces: number | null): string {
  if (workspaces === null) {
    return "The token is accepted.";
  }
  return `The token is accepted and may read ${countNoun(workspaces, "workspace")}.`;
}

export function describeTokenCheck(check: ConnectionCheck): Finding {
  switch (check.token) {
    case "missing":
      return { tone: "warn", text: "No token is stored for this connection." };
    case "accepted":
      return { tone: "good", text: describeAccepted(check.workspaces) };
    case "refused":
      return { tone: "bad", text: "The server refused the token: unknown, revoked or expired." };
    case "no_read_scope":
      return {
        tone: "warn",
        text: "The token is valid but has no read scope. Status needs read; Tokens needs admin.",
      };
    case "unknown":
      return { tone: "warn", text: "The token could not be checked." };
  }
}
