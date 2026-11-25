import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { AppError } from "../ipc/app-error";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { ErrorNotice, Notice } from "../ui/notice";
import { Panel, Screen } from "../ui/panel";
import { useLoad } from "../ui/use-load";
import { CreatedTokenPanel } from "./created-token";
import { TokenForm } from "./token-form";
import { TokenTable } from "./token-table";
import type { CreatedToken, TokenInfo } from "./tokens";
import { listTokens, listWorkspaceNames } from "./tokens-api";

export interface TokensScreenProps {
  readonly connection: Connection;
}

interface TokensData {
  readonly tokens: readonly TokenInfo[];
  /** The workspaces a new token can be limited to; empty when the token may not list them. */
  readonly workspaces: readonly string[];
}

/** Scopes do not imply each other: an admin token without `read` cannot list workspaces. */
async function readWorkspaces(backend: Backend, id: string): Promise<readonly string[]> {
  try {
    return await listWorkspaceNames(backend, id);
  } catch (error) {
    if (error instanceof AppError && error.kind === "denied") {
      return [];
    }
    throw error;
  }
}

async function readTokens(backend: Backend, id: string): Promise<TokensData> {
  const tokens = await listTokens(backend, id);
  return { tokens, workspaces: await readWorkspaces(backend, id) };
}

export function TokensScreen({ connection }: TokensScreenProps): ReactElement {
  return (
    <Screen
      title="Tokens"
      lead="The tokens of this server: who may read, write or administer. Needs a token with the admin scope."
    >
      {connection.hasToken ? (
        <Tokens connection={connection} />
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <p>Store a token with the admin scope on the Connection screen.</p>
        </Notice>
      )}
    </Screen>
  );
}

function Tokens({ connection }: TokensScreenProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readTokens(backend, connection.id), [backend, connection.id]),
  );
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const { reload } = loaded;
  const onCreated = useCallback(
    (token: CreatedToken): void => {
      setCreated(token);
      reload();
    },
    [reload],
  );
  const data = loaded.state.phase === "ready" ? loaded.state.data : null;
  return (
    <>
      {created === null ? null : (
        <CreatedTokenPanel
          created={created}
          onDone={() => {
            setCreated(null);
          }}
        />
      )}
      {data === null ? null : (
        <TokenForm connection={connection} workspaces={data.workspaces} onCreated={onCreated} />
      )}
      <Panel
        title="Tokens"
        actions={
          <button type="button" className="button" disabled={loaded.busy} onClick={reload}>
            {loaded.busy ? "Refreshing…" : "Refresh"}
          </button>
        }
      >
        {loaded.state.phase === "loading" ? <p className="muted">Asking the server…</p> : null}
        {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
        {data === null ? null : (
          <TokenTable connection={connection} tokens={data.tokens} onChanged={reload} />
        )}
      </Panel>
    </>
  );
}
