// The token commands of the Rust side. Rust adds the connection's own token to each call; the
// front end passes only the connection's id.

import type { Backend } from "../ipc/backend";
import {
  parseCreatedToken,
  parseTokenList,
  parseWorkspaceNames,
  toNewTokenRequest,
} from "./tokens";
import type { CreatedToken, NewTokenForm, TokenInfo } from "./tokens";

export async function listTokens(backend: Backend, id: string): Promise<readonly TokenInfo[]> {
  return parseTokenList(await backend.invoke("api_tokens_list", { id }));
}

export async function listWorkspaceNames(backend: Backend, id: string): Promise<readonly string[]> {
  return parseWorkspaceNames(await backend.invoke("api_workspaces", { id }));
}

/** Creates a token. The answer holds its secret: the one time a token reaches the front end. */
export async function createToken(
  backend: Backend,
  id: string,
  form: NewTokenForm,
): Promise<CreatedToken> {
  const request = toNewTokenRequest(form);
  return parseCreatedToken(await backend.invoke("api_token_create", { id, request }));
}

export async function revokeToken(backend: Backend, id: string, tokenId: string): Promise<void> {
  await backend.invoke("api_token_revoke", { id, tokenId });
}
