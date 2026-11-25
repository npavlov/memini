// Tokens as the API lists them: a name, a display prefix, scopes, never the secret. The secret
// exists in one answer only, the creation of a token, and is shown once.

import type { CommandArguments } from "../ipc/backend";
import { ShapeError, readArray, readObject, readString, readStrings } from "../json/read-json";
import { displayText } from "../text/display-text";
import type { Tone } from "../components";

export const SCOPES = ["read", "ingest", "admin", "sensitive"] as const;

export type Scope = (typeof SCOPES)[number];

export const SCOPE_MEANINGS: Readonly<Record<Scope, string>> = {
  read: "Search and read: what an agent needs over MCP.",
  ingest: "Write content: push records, record decisions.",
  admin: "Manage workspaces, sources and tokens.",
  sensitive: "With read: also records marked sensitive, when a read asks for them.",
};

export interface TokenInfo {
  readonly id: string;
  readonly name: string;
  readonly prefix: string;
  readonly scopes: readonly string[];
  /** The workspace the token is limited to; null: every workspace. */
  readonly workspace: string | null;
  readonly status: string;
  readonly createdAt: string | null;
  readonly expiresAt: string | null;
  readonly revokedAt: string | null;
  readonly lastUsedAt: string | null;
}

export interface CreatedToken {
  readonly info: TokenInfo;
  /** The secret. Held only while it is shown. */
  readonly secret: string;
}

function parseTokenInfo(value: unknown): TokenInfo {
  const token = readObject(value, "a token of the list");
  const id = readString(token["id"]);
  if (id === null) {
    throw new ShapeError("a token with an id");
  }
  const workspace = readString(token["workspace"]);
  return {
    id,
    name: displayText(readString(token["name"]), 200),
    prefix: displayText(readString(token["prefix"]), 40),
    scopes: readStrings(token["scopes"]).map((scope) => displayText(scope, 20)),
    workspace: workspace === null ? null : displayText(workspace, 80),
    status: displayText(readString(token["status"]), 20),
    createdAt: readString(token["created_at"]),
    expiresAt: readString(token["expires_at"]),
    revokedAt: readString(token["revoked_at"]),
    lastUsedAt: readString(token["last_used_at"]),
  };
}

export function parseTokenList(value: unknown): readonly TokenInfo[] {
  const list = readObject(value, "the list of tokens");
  return readArray(list["tokens"]).map(parseTokenInfo);
}

export function parseCreatedToken(value: unknown): CreatedToken {
  const secret = readString(readObject(value, "a created token")["token"]);
  if (secret === null || !SECRET.test(secret)) {
    throw new ShapeError("a created token with its secret");
  }
  return { info: parseTokenInfo(value), secret };
}

/** The names of the workspaces of `GET /v1/workspaces`. */
export function parseWorkspaceNames(value: unknown): readonly string[] {
  const list = readObject(value, "the list of workspaces");
  return readArray(list["workspaces"])
    .map((item) => readString(readObject(item, "a workspace")["name"]))
    .filter((name): name is string => name !== null && HANDLE.test(name));
}

export function tokenTone(status: string): Tone {
  if (status === "active") {
    return "good";
  }
  return status === "expired" ? "warn" : "idle";
}

export const EXPIRIES = [
  { id: "never", label: "Never", days: null },
  { id: "7d", label: "In 7 days", days: 7 },
  { id: "30d", label: "In 30 days", days: 30 },
  { id: "90d", label: "In 90 days", days: 90 },
  { id: "365d", label: "In a year", days: 365 },
] as const;

export type ExpiryId = (typeof EXPIRIES)[number]["id"];

export interface NewTokenForm {
  readonly name: string;
  readonly scopes: readonly Scope[];
  /** A workspace's name; empty: every workspace. */
  readonly workspace: string;
  readonly expiry: ExpiryId;
}

export const EMPTY_TOKEN_FORM: NewTokenForm = {
  name: "",
  scopes: ["read"],
  workspace: "",
  expiry: "never",
};

const MAX_NAME_LENGTH = 200;
/** What may be shown as a new token: printable ASCII of a bounded length. */
const SECRET = /^[\x21-\x7E]{1,200}$/u;
const HANDLE = /^[a-z0-9][a-z0-9_-]{0,62}$/u;
const UNPRINTABLE = /[\u0000-\u001F\u007F-\u009F]/u;
const SECONDS_PER_DAY = 86_400;

/** The first problem with the form, or null. A hint for the user; the server checks again. */
export function checkNewToken(form: NewTokenForm): string | null {
  const name = form.name.trim();
  if (name === "") {
    return "Say what the token is for, such as “laptop agent”.";
  }
  if (Array.from(name).length > MAX_NAME_LENGTH || UNPRINTABLE.test(name)) {
    return `A token's name is at most ${String(MAX_NAME_LENGTH)} printable characters.`;
  }
  if (form.scopes.length === 0) {
    return "Choose at least one scope.";
  }
  if (form.workspace !== "" && !HANDLE.test(form.workspace)) {
    return "A workspace's name is lowercase letters, digits, '-' or '_'.";
  }
  return null;
}

export function toggleScope(scopes: readonly Scope[], scope: Scope): readonly Scope[] {
  const wanted = scopes.includes(scope)
    ? scopes.filter((item) => item !== scope)
    : [...scopes, scope];
  return SCOPES.filter((item) => wanted.includes(item));
}

/** The form as the `api_token_create` command takes it. */
export function toNewTokenRequest(form: NewTokenForm): CommandArguments {
  const days = EXPIRIES.find((expiry) => expiry.id === form.expiry)?.days ?? null;
  return {
    name: form.name.trim(),
    scopes: SCOPES.filter((scope) => form.scopes.includes(scope)),
    workspace: form.workspace === "" ? null : form.workspace,
    expires_in_seconds: days === null ? null : days * SECONDS_PER_DAY,
  };
}
