// The connection commands of the Rust side. A token is never an argument and never an answer:
// it is read by Rust, from the clipboard or a file, and goes to the system keychain.

import type { Backend } from "../ipc/backend";
import { parseConnectionCheck } from "./connection-check";
import type { ConnectionCheck } from "./connection-check";
import { parseConnectionsView, toConnectionInput } from "./connections";
import type { ConnectionForm, ConnectionsView } from "./connections";

export async function listConnections(backend: Backend): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connections_list"));
}

export async function saveConnection(
  backend: Backend,
  form: ConnectionForm,
): Promise<ConnectionsView> {
  const input = toConnectionInput(form);
  return parseConnectionsView(await backend.invoke("connection_save", { input }));
}

export async function deleteConnection(backend: Backend, id: string): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connection_delete", { id }));
}

export async function activateConnection(backend: Backend, id: string): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connection_activate", { id }));
}

export async function takeTokenFromClipboard(
  backend: Backend,
  id: string,
): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connection_token_from_clipboard", { id }));
}

export async function takeTokenFromFile(
  backend: Backend,
  id: string,
  path: string,
): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connection_token_from_file", { id, path }));
}

export async function clearToken(backend: Backend, id: string): Promise<ConnectionsView> {
  return parseConnectionsView(await backend.invoke("connection_token_clear", { id }));
}

export async function checkConnection(backend: Backend, id: string): Promise<ConnectionCheck> {
  return parseConnectionCheck(await backend.invoke("connection_check", { id }));
}
