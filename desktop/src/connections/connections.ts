// Saved connections as the front end sees them: a name, an address and whether a token is stored.
// Never the token. The checks here give the form its hints; the Rust side decides.

import type { CommandArguments } from "../ipc/backend";
import { ShapeError, readArray, readBoolean, readObject, readString } from "../json/read-json";

export type ConnectionKind = "local" | "remote";

export interface Connection {
  readonly id: string;
  readonly name: string;
  readonly kind: ConnectionKind;
  readonly url: string;
  readonly allowHttp: boolean;
  readonly hasToken: boolean;
}

export interface ConnectionsView {
  readonly connections: readonly Connection[];
  readonly activeId: string | null;
}

export interface ConnectionForm {
  readonly id: string | null;
  readonly name: string;
  readonly kind: ConnectionKind;
  readonly url: string;
  readonly allowHttp: boolean;
}

const ADDRESS_EXAMPLE = "Enter the server's address, such as https://memini.example.org.";

function parseConnection(value: unknown): Connection {
  const item = readObject(value, "a connection");
  const id = readString(item["id"]);
  const url = readString(item["url"]);
  if (id === null || url === null) {
    throw new ShapeError("a connection with an id and an address");
  }
  return {
    id,
    url,
    name: readString(item["name"]) ?? "",
    kind: item["kind"] === "local" ? "local" : "remote",
    allowHttp: readBoolean(item["allow_http"]) ?? false,
    hasToken: readBoolean(item["has_token"]) ?? false,
  };
}

export function parseConnectionsView(value: unknown): ConnectionsView {
  const view = readObject(value, "the list of connections");
  return {
    connections: readArray(view["connections"]).map(parseConnection),
    activeId: readString(view["active_id"]),
  };
}

export function activeConnection(view: ConnectionsView): Connection | null {
  return view.connections.find((connection) => connection.id === view.activeId) ?? null;
}

export function localConnection(view: ConnectionsView): Connection | null {
  return view.connections.find((connection) => connection.kind === "local") ?? null;
}

function parseAddress(text: string): URL | null {
  try {
    return new URL(text.trim());
  } catch (error) {
    if (error instanceof TypeError) {
      return null;
    }
    throw error;
  }
}

function isLoopbackHost(hostname: string): boolean {
  const host = hostname.toLowerCase();
  return host === "localhost" || host === "[::1]" || /^127(?:\.\d{1,3}){3}$/u.test(host);
}

/** Whether the address names this computer. */
export function isLoopbackAddress(address: string): boolean {
  const url = parseAddress(address);
  return url !== null && isLoopbackHost(url.hostname);
}

/** Whether a token sent to the address could be read on the network. */
export function isPlainHttpToAnotherComputer(address: string): boolean {
  const url = parseAddress(address);
  return url !== null && url.protocol === "http:" && !isLoopbackHost(url.hostname);
}

function checkAddress(form: ConnectionForm): string | null {
  const url = parseAddress(form.url);
  if (url === null || (url.protocol !== "http:" && url.protocol !== "https:")) {
    return ADDRESS_EXAMPLE;
  }
  if (url.username !== "" || url.password !== "") {
    return "Remove the user name and password from the address: the token is the credential.";
  }
  if (url.search !== "" || url.hash !== "") {
    return "The address must not have a query or a fragment.";
  }
  if (form.kind === "local" && !isLoopbackHost(url.hostname)) {
    return "A local connection is a server on this computer: 127.0.0.1, localhost or ::1.";
  }
  if (form.kind === "remote" && isPlainHttpToAnotherComputer(form.url) && !form.allowHttp) {
    return "Plain HTTP lets the network read the token. Use https://, or allow plain HTTP below.";
  }
  return null;
}

/** The first problem with the form, or null. A hint for the user; Rust checks again. */
export function checkConnectionForm(form: ConnectionForm): string | null {
  if (form.name.trim() === "") {
    return "Give the connection a name.";
  }
  return checkAddress(form);
}

export function formOf(connection: Connection): ConnectionForm {
  return {
    id: connection.id,
    name: connection.name,
    kind: connection.kind,
    url: connection.url,
    allowHttp: connection.allowHttp,
  };
}

export function newForm(kind: ConnectionKind, localUrl: string): ConnectionForm {
  const local = kind === "local";
  return {
    id: null,
    name: local ? "This computer" : "",
    kind,
    url: local ? localUrl : "https://",
    allowHttp: false,
  };
}

/** The form as the `connection_save` command takes it. */
export function toConnectionInput(form: ConnectionForm): CommandArguments {
  return {
    id: form.id,
    name: form.name.trim(),
    kind: form.kind,
    url: form.url.trim(),
    allow_http: form.kind === "remote" && form.allowHttp,
  };
}

/** Whether the form's address is another origin (scheme, host or port) than the saved one. */
export function changesOrigin(form: ConnectionForm, saved: Connection | null): boolean {
  if (saved === null) {
    return false;
  }
  const before = parseAddress(saved.url);
  const after = parseAddress(form.url);
  return before === null || after === null || before.origin !== after.origin;
}

/**
 * Whether saving the form removes the connection's token. A token belongs to the server it was
 * stored for, so another origin drops it; the local connection keeps it between addresses on this
 * computer, because the launcher's server can come back on another port.
 */
export function dropsToken(form: ConnectionForm, saved: Connection | null): boolean {
  if (saved === null || !saved.hasToken || !changesOrigin(form, saved)) {
    return false;
  }
  const staysLocal =
    saved.kind === "local" &&
    form.kind === "local" &&
    isLoopbackAddress(saved.url) &&
    isLoopbackAddress(form.url);
  return !staysLocal;
}
