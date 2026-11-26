import { useCallback } from "react";
import type { ReactElement } from "react";

import { ActionOutcome, ConfirmButton, DataTable, Mono, Pill, Text } from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { formatInstant } from "../text/format-time";
import { tokenTone } from "./tokens";
import type { TokenInfo } from "./tokens";
import { revokeToken } from "./tokens-api";

export interface TokenTableProps {
  readonly connection: Connection;
  readonly tokens: readonly TokenInfo[];
  readonly onChanged: () => void;
}

const TOKEN_COLUMNS = [
  "Name",
  "Starts with",
  "Scopes",
  "Workspace",
  "State",
  "Created",
  "Last used",
  "Expires",
];

export function TokenTable({ connection, tokens, onChanged }: TokenTableProps): ReactElement {
  const backend = useBackend();
  const revoke = useCallback(
    async (tokenId: string): Promise<string> => {
      await revokeToken(backend, connection.id, tokenId);
      onChanged();
      return "Revoked: whatever used the token can no longer reach the server with it.";
    },
    [backend, connection.id, onChanged],
  );
  const revoking = useAction(revoke);
  if (tokens.length === 0) {
    return <Text tone="muted">The server lists no token this one may see.</Text>;
  }
  return (
    <>
      <ActionOutcome of={revoking} />
      <DataTable columns={TOKEN_COLUMNS} withActions>
        {tokens.map((token) => (
          <TokenRow
            key={token.id}
            token={token}
            disabled={revoking.pending}
            onRevoke={() => revoking.run(token.id)}
          />
        ))}
      </DataTable>
    </>
  );
}

interface TokenRowProps {
  readonly token: TokenInfo;
  readonly disabled: boolean;
  readonly onRevoke: () => Promise<void>;
}

function TokenRow({ token, disabled, onRevoke }: TokenRowProps): ReactElement {
  return (
    <tr>
      <th scope="row">{token.name}</th>
      <td>
        <Mono>{token.prefix}…</Mono>
      </td>
      <td>{token.scopes.join(", ")}</td>
      <td>{token.workspace ?? "every workspace"}</td>
      <td>
        <Pill tone={tokenTone(token.status)}>{token.status}</Pill>
      </td>
      <td>{formatInstant(token.createdAt)}</td>
      <td>{token.lastUsedAt === null ? "never" : formatInstant(token.lastUsedAt)}</td>
      <td>{token.expiresAt === null ? "never" : formatInstant(token.expiresAt)}</td>
      <td>
        {token.status === "active" ? (
          <ConfirmButton
            label="Revoke"
            question="Revoke this token? Whatever uses it stops working."
            confirmLabel="Revoke"
            working="Revoking…"
            disabled={disabled}
            onConfirm={onRevoke}
          />
        ) : null}
      </td>
    </tr>
  );
}
