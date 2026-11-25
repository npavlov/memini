// The agent clients on this computer, as the Rust side reads them from each client's own
// configuration file. Connect and Disconnect run in Rust: it makes and revokes the client's
// token and writes the file. No token comes here; the front end names a client and shows what
// the file says.

import type { Tone } from "../components";
import type { Backend, CommandArguments } from "../ipc/backend";
import { readArray, readBoolean, readObject, readString } from "../json/read-json";
import type { JsonObject } from "../json/read-json";
import { shellQuote } from "./mcp-clients";
import type { Snippet } from "./mcp-clients";

/** How the entry of a client gives the token: by its variable, in the file, or not at all. */
export type TokenForm = "reference" | "literal" | "none";

export type ClientStatus =
  | { readonly state: "absent" }
  | { readonly state: "connected"; readonly url: string | null; readonly token: TokenForm }
  | { readonly state: "unreadable"; readonly message: string };

export interface ClientVariant {
  readonly id: string;
  readonly label: string;
}

export interface AgentClient {
  readonly id: string;
  readonly name: string;
  /** The client's own configuration file. */
  readonly file: string;
  readonly status: ClientStatus;
  /** The environment variable the client's entry can refer to; null: it takes a token only. */
  readonly variable: string | null;
  /** Whether the app can write the token itself into the client's file. */
  readonly takesLiteral: boolean;
  /** Where the app keeps the token it made for the client. */
  readonly tokenFile: string;
  readonly hasTokenFile: boolean;
  /** The layouts the client's file can have, when it has more than one. */
  readonly variants: readonly ClientVariant[];
  /** The layout the file is in, when it tells. */
  readonly variant: string | null;
  /** Whether the app has a token on record that it made for this client. */
  readonly hasToken: boolean;
}

function parseTokenForm(value: unknown): TokenForm {
  return value === "reference" || value === "literal" ? value : "none";
}

function parseStatus(client: JsonObject): ClientStatus {
  switch (readString(client["state"])) {
    case "connected":
      return { state: "connected", url: readString(client["url"]), token: parseTokenForm(client["token"]) };
    case "unreadable":
      return { state: "unreadable", message: readString(client["message"]) ?? "" };
    default:
      return { state: "absent" };
  }
}

function parseVariant(value: unknown): ClientVariant {
  const variant = readObject(value, "a layout of a client's file");
  return { id: readString(variant["id"]) ?? "", label: readString(variant["label"]) ?? "" };
}

export function parseAgentClient(value: unknown): AgentClient {
  const client = readObject(value, "an agent client");
  return {
    id: readString(client["id"]) ?? "",
    name: readString(client["name"]) ?? "",
    file: readString(client["file"]) ?? "",
    status: parseStatus(client),
    variable: readString(client["variable"]),
    takesLiteral: readBoolean(client["takes_literal"]) ?? false,
    tokenFile: readString(client["token_file"]) ?? "",
    hasTokenFile: readBoolean(client["has_token_file"]) ?? false,
    variants: readArray(client["variants"]).map(parseVariant),
    variant: readString(client["variant"]),
    hasToken: readBoolean(client["has_token"]) ?? false,
  };
}

/** What became of the token the app made for a client. */
export type Revoked =
  | { readonly result: "done" }
  | { readonly result: "nothing" }
  | { readonly result: "failed"; readonly message: string };

export interface Disconnected {
  readonly client: AgentClient;
  /** Whether the file had the entry. */
  readonly removed: boolean;
  readonly revoked: Revoked;
}

function parseRevoked(value: unknown): Revoked {
  const revoked = readObject(value, "what became of the client's token");
  switch (readString(revoked["result"])) {
    case "done":
      return { result: "done" };
    case "failed":
      return { result: "failed", message: readString(revoked["message"]) ?? "" };
    default:
      return { result: "nothing" };
  }
}

export function parseDisconnected(value: unknown): Disconnected {
  const answer = readObject(value, "the answer to Disconnect");
  return {
    client: parseAgentClient(answer["client"]),
    removed: readBoolean(answer["removed"]) ?? false,
    revoked: parseRevoked(answer["revoked"]),
  };
}

/** What the user chose for Connect. */
export interface ConnectChoice {
  /** The workspace the client's token may read; empty: every workspace. */
  readonly workspace: string;
  /** Write the token itself into the client's file. */
  readonly literal: boolean;
  /** The layout of the client's file, for a client whose file does not tell; empty: none. */
  readonly variant: string;
}

/** Whether Connect writes the token itself into the client's file: by choice, or for want of a variable. */
export function writesLiteral(client: AgentClient, choice: ConnectChoice): boolean {
  return client.variable === null || (choice.literal && client.takesLiteral);
}

/** Whether the user must name the layout of the client's file before Connect. */
export function needsVariant(client: AgentClient): boolean {
  return client.variants.length > 0 && client.variant === null;
}

/** Why Connect cannot run yet, or null. */
export function connectProblem(client: AgentClient, choice: ConnectChoice): string | null {
  if (needsVariant(client) && !client.variants.some((variant) => variant.id === choice.variant)) {
    return `Choose the version of ${client.name}: its file does not tell.`;
  }
  return null;
}

/** What the app says before it writes a token into a client's file. */
export function literalQuestion(client: AgentClient): string {
  const reason =
    client.variable === null ? `${client.name} takes only a token written into its file. ` : "";
  return `${reason}The token will be in ${client.file} as plain text: whoever can read that file can read what the token reads. Write it?`;
}

/** What Connect will do for this client with this choice, said before the click. */
export function connectNote(client: AgentClient, choice: ConnectChoice): string {
  if (writesLiteral(client, choice)) {
    return `Connect makes a read token for ${client.name} and writes it into the client’s file, so ${client.name} works however it is started.`;
  }
  return `Connect makes a read token for ${client.name}, keeps it in a file only you can read, and writes an entry that reads ${client.variable ?? "a variable"}. ${client.name} must be started from a shell that loaded the variable: the line for it appears here.`;
}

export interface ClientSummary {
  readonly tone: Tone;
  readonly label: string;
  readonly text: string;
}

function tokenText(client: AgentClient, token: TokenForm): string {
  switch (token) {
    case "literal":
      return "Its token is written in the client’s file.";
    case "reference":
      return "The client reads its token from an environment variable.";
    case "none":
      return `The entry has no token, so ${client.name} is refused by the server.`;
  }
}

/**
 * The state of a client in words, from what its file says. `address` is the MCP address of the
 * connection on screen: an entry for another server is said to be one.
 */
export function describeClient(client: AgentClient, address: string | null): ClientSummary {
  const status = client.status;
  switch (status.state) {
    case "absent":
      return { tone: "idle", label: "Not connected", text: "Its file has no entry named memini." };
    case "unreadable":
      return { tone: "bad", label: "Not readable", text: status.message };
    case "connected": {
      const here = status.url !== null && status.url === address;
      return here && status.token !== "none"
        ? { tone: "good", label: "Connected", text: tokenText(client, status.token) }
        : {
            tone: "warn",
            label: here ? "Connected without a token" : "Connected to another address",
            text: here ? tokenText(client, status.token) : (status.url ?? "The entry has no address."),
          };
    }
  }
}

function powershellQuote(text: string): string {
  return `'${text.replaceAll("'", "''")}'`;
}

/**
 * The lines that load the client's token into its variable, for the shell that starts the
 * client. Empty when the client has no variable or the app keeps no token file for it.
 */
export function loadSnippets(client: AgentClient): readonly Snippet[] {
  if (client.variable === null || !client.hasTokenFile) {
    return [];
  }
  return [
    {
      label: "Bash or Zsh",
      language: "sh",
      text: `export ${client.variable}="$(cat ${shellQuote(client.tokenFile)})"`,
    },
    {
      label: "PowerShell",
      language: "powershell",
      text: `$env:${client.variable} = (Get-Content -Raw ${powershellQuote(client.tokenFile)}).Trim()`,
    },
  ];
}

/** What to tell the user after Disconnect. */
export function describeDisconnected(outcome: Disconnected): ClientSummary {
  const name = outcome.client.name;
  const entry = outcome.removed ? "The entry was removed" : "The file had no entry";
  switch (outcome.revoked.result) {
    case "done":
      return { tone: "good", label: `${name} is disconnected`, text: `${entry}, and its token is revoked.` };
    case "nothing":
      return {
        tone: "info",
        label: `${name} is disconnected`,
        text: `${entry}. The app made no token for it, so none was revoked: if the entry held a token, revoke it on the Tokens screen.`,
      };
    case "failed":
      return {
        tone: "warn",
        label: `The token of ${name} is still valid`,
        text: `${entry}, but its token could not be revoked. ${outcome.revoked.message}`,
      };
  }
}

export async function listAgentClients(backend: Backend): Promise<readonly AgentClient[]> {
  return readArray(await backend.invoke("clients_list")).map(parseAgentClient);
}

/** Where Connect points a client: a saved connection, by its id. */
export interface ConnectTarget {
  readonly client: AgentClient;
  readonly connection: string;
}

export function toConnectRequest(target: ConnectTarget, choice: ConnectChoice): CommandArguments {
  return {
    client: target.client.id,
    connection: target.connection,
    workspace: choice.workspace === "" ? null : choice.workspace,
    literal: writesLiteral(target.client, choice),
    variant: choice.variant === "" ? null : choice.variant,
  };
}

export async function connectClient(
  backend: Backend,
  target: ConnectTarget,
  choice: ConnectChoice,
): Promise<AgentClient> {
  const request = toConnectRequest(target, choice);
  return parseAgentClient(await backend.invoke("client_connect", { request }));
}

export async function disconnectClient(backend: Backend, client: string): Promise<Disconnected> {
  return parseDisconnected(await backend.invoke("client_disconnect", { client }));
}
