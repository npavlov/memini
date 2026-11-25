// What a test of a connection found, step by step: whether the address answers, the version
// the server reports, whether a token is stored, whether the server accepts it, and what the
// token may do. A step that fails does not hide the steps before it, and says what to do next.

import type { Step, Tone } from "../components";
import { toAppError } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";
import { readBoolean, readNumber, readObject, readString, readStrings } from "../json/read-json";
import { countNoun } from "../text/count-noun";
import { displayText } from "../text/display-text";
import type { Connection } from "./connections";

export const TOKEN_CHECKS = ["missing", "accepted", "refused", "lacks_scope", "unknown", "not_checked"] as const;

/**
 * What is known about the token: none is stored; the server accepted it, refused it, or wants a
 * scope it lacks; the server's answer said nothing about it; or the server did not answer, so
 * the token was not tried.
 */
export type TokenCheck = (typeof TOKEN_CHECKS)[number];

export interface ConnectionCheck {
  /** Why the address did not answer; null: it answered. */
  readonly unreachable: AppError | null;
  /** The version the server reports; null when the address answers without one. */
  readonly version: string | null;
  readonly ready: boolean | null;
  readonly token: TokenCheck;
  /** The scope the token lacks, when the server refused the test's read for that. */
  readonly lacking: string | null;
  /** The token's scopes, when the API tells them: it does to a token with the admin scope. */
  readonly scopes: readonly string[] | null;
  readonly workspaces: number | null;
  /** The server's words when the token was not accepted. */
  readonly detail: string | null;
}

function readTokenCheck(value: unknown): TokenCheck {
  return TOKEN_CHECKS.find((check) => check === value) ?? "unknown";
}

export function parseConnectionCheck(value: unknown): ConnectionCheck {
  const check = readObject(value, "the result of a connection test");
  const unreachable = check["unreachable"] ?? null;
  const version = displayText(readString(check["version"]), 40);
  return {
    unreachable: unreachable === null ? null : toAppError(unreachable),
    version: version === "" ? null : version,
    ready: readBoolean(check["ready"]),
    token: readTokenCheck(check["token"]),
    lacking: readString(check["lacking"]),
    scopes: Array.isArray(check["scopes"]) ? readStrings(check["scopes"]) : null,
    workspaces: readNumber(check["workspaces"]),
    detail: readString(check["detail"]),
  };
}

const ADD_TOKEN = "Add a token: paste it or choose a file.";
const REPLACE_TOKEN = "Replace it: paste a new token or choose a file.";

function addressStep(check: ConnectionCheck): Step {
  if (check.unreachable === null) {
    return { id: "address", title: "The address answers", state: "done" };
  }
  return {
    id: "address",
    title: "The address does not answer",
    state: "failed",
    detail: `${check.unreachable.message} Check the address and that the server is running; the server of this computer is started on the Server screen.`,
  };
}

function versionStep(check: ConnectionCheck): Step {
  if (check.unreachable !== null) {
    return { id: "version", title: "The version of the server", state: "waiting", detail: "Not asked: the address does not answer." };
  }
  if (check.version === null) {
    return {
      id: "version",
      title: "No Memini server at this address",
      state: "failed",
      detail: "Something answers there, but it reports no version as a Memini server does. Check the address and its port.",
    };
  }
  const waking = check.ready === false ? "It is not ready yet: it is starting, or its database does not answer." : null;
  return { id: "version", title: `Memini ${check.version}`, state: "done", detail: waking };
}

function storedStep(check: ConnectionCheck): Step {
  return check.token === "missing"
    ? { id: "stored", title: "No token", state: "failed", detail: ADD_TOKEN }
    : { id: "stored", title: "A token is stored", state: "done" };
}

function refusedDetail(check: ConnectionCheck): string {
  const said = displayText(check.detail);
  return said === "" ? "" : ` The server said: ${said}`;
}

function acceptedStep(check: ConnectionCheck): Step {
  const id = "accepted";
  switch (check.token) {
    case "missing":
      return { id, title: "The server accepts the token", state: "waiting", detail: "Nothing to try: no token is stored." };
    case "not_checked":
      return { id, title: "The server accepts the token", state: "waiting", detail: "Not tried: the address does not answer." };
    case "accepted": {
      const reads = check.workspaces === null ? null : `It may read ${countNoun(check.workspaces, "workspace")}.`;
      return { id, title: "The server accepts the token", state: "done", detail: reads };
    }
    case "refused":
      return {
        id,
        title: "Token refused",
        state: "failed",
        detail: `The server does not know this token, or it is revoked or expired. ${REPLACE_TOKEN}`,
      };
    case "lacks_scope":
      return {
        id,
        title: `The token lacks the ${check.lacking ?? "needed"} scope`,
        state: "failed",
        detail: "The token is valid, but the status of the base is read with the read scope. Use a token that has it: one with the admin scope creates it on the Tokens screen.",
      };
    case "unknown":
      return { id, title: "The token could not be tried", state: "failed", detail: `Test again in a moment.${refusedDetail(check)}` };
  }
}

const CLOSED: Readonly<Record<string, string>> = {
  read: "Without read, Data, the project map and the status of the base are closed.",
  ingest: "Without ingest, the project map cannot be saved.",
  admin: "Without admin, the Tokens screen and Connect for agent clients are closed.",
};

function scopesStep(check: ConnectionCheck): Step | null {
  if (check.scopes !== null) {
    const closed = Object.entries(CLOSED)
      .filter(([scope]) => !check.scopes?.includes(scope))
      .map(([, text]) => text);
    return {
      id: "scopes",
      title: `Scopes: ${check.scopes.length === 0 ? "none" : check.scopes.join(", ")}`,
      state: "done",
      detail: closed.length === 0 ? null : closed.join(" "),
    };
  }
  if (check.token !== "accepted" && check.token !== "lacks_scope") {
    return null;
  }
  return {
    id: "scopes",
    title: "The scopes of the token",
    state: "waiting",
    detail: "Not told: the server lists a token’s scopes only for a token with the admin scope.",
  };
}

/** The test as steps in their order, each with its state and, where it failed, what to do next. */
export function checkSteps(check: ConnectionCheck): readonly Step[] {
  const steps = [addressStep(check), versionStep(check), storedStep(check), acceptedStep(check), scopesStep(check)];
  return steps.filter((step) => step !== null);
}

/** Why a connection cannot show data: its token is missing, or the server refused it. */
export interface TokenProblem {
  readonly kind: "missing" | "refused";
  readonly tone: Tone;
  /** A few words, the same in the status bar and on a screen. */
  readonly label: string;
  readonly text: string;
  /** What the button that leads to the token says. */
  readonly action: string;
}

const NO_TOKEN: TokenProblem = {
  kind: "missing",
  tone: "warn",
  label: "No token",
  text: `This connection has no token, so the server gives the app nothing to show. ${ADD_TOKEN}`,
  action: "Add a token",
};

const TOKEN_REFUSED: TokenProblem = {
  kind: "refused",
  tone: "bad",
  label: "Token refused",
  text: `The server refused the stored token: it does not know it, or it is revoked or expired. ${REPLACE_TOKEN}`,
  action: "Replace the token",
};

/**
 * What stands between a connection and its data, or null. A connection without a token is
 * known without asking the server; a refused one is known once the server was asked.
 */
export function tokenProblem(connection: Connection, token: TokenCheck | null): TokenProblem | null {
  if (!connection.hasToken || token === "missing") {
    return NO_TOKEN;
  }
  return token === "refused" ? TOKEN_REFUSED : null;
}

export interface Finding {
  readonly tone: Tone;
  readonly text: string;
}

/** The state of the token in a sentence, for where the base cannot be read. */
export function describeToken(token: TokenCheck, lacking: string | null): Finding {
  switch (token) {
    case "missing":
      return { tone: NO_TOKEN.tone, text: NO_TOKEN.text };
    case "refused":
      return { tone: TOKEN_REFUSED.tone, text: TOKEN_REFUSED.text };
    case "lacks_scope":
      return { tone: "warn", text: `The token is valid but lacks the ${lacking ?? "read"} scope.` };
    case "accepted":
      return { tone: "good", text: "The server accepts the token." };
    case "unknown":
    case "not_checked":
      return { tone: "warn", text: "The token could not be tried." };
  }
}
