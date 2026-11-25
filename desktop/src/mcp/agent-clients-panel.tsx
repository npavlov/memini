import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Button, EmptyState, ErrorNotice, Field, Notice, Panel, Select, Text } from "../components";
import type { TokenProblem } from "../connections/connection-check";
import type { Connection } from "../connections/connections";
import { TokenNotice } from "../connections/token-needed";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { listWorkspaceNames } from "../tokens/tokens-api";
import { AgentClientCard } from "./agent-client-card";
import { listAgentClients } from "./agent-clients";
import type { AgentClient, ClientSummary } from "./agent-clients";
import { mcpAddressOf } from "./mcp-clients";

export interface AgentClientsPanelProps {
  readonly connection: Connection;
  /** Connect makes a token with the connection's own: what is wrong with that one, if anything. */
  readonly token: { readonly problem: TokenProblem | null; readonly onAddToken: () => void };
}

const EVERY_WORKSPACE = { value: "", label: "Every workspace" };

/**
 * The agent clients found on this computer. Each is connected or disconnected on its own click;
 * what is shown is read from the client's file every time, not remembered.
 */
export function AgentClientsPanel({ connection, token }: AgentClientsPanelProps): ReactElement {
  const backend = useBackend();
  const clients = useLoad(useCallback(() => listAgentClients(backend), [backend]));
  const workspaces = useLoad(
    useCallback(() => listWorkspaceNames(backend, connection.id), [backend, connection.id]),
  );
  const [workspace, setWorkspace] = useState("");
  const [outcome, setOutcome] = useState<ClientSummary | null>(null);
  const reload = clients.reload;
  const changed = useCallback(
    (next: ClientSummary | null): void => {
      setOutcome(next);
      reload();
    },
    [reload],
  );
  const names = workspaces.state.phase === "ready" ? workspaces.state.data : [];
  return (
    <Panel
      title="Agent clients on this computer"
      actions={
        <Button disabled={clients.busy} onClick={reload}>
          {clients.busy ? "Looking…" : "Look again"}
        </Button>
      }
    >
      {token.problem === null ? null : <TokenNotice problem={token.problem} onAddToken={token.onAddToken} />}
      <WorkspaceChoice names={names} value={workspace} onChange={setWorkspace} />
      {outcome === null ? null : (
        <Notice tone={outcome.tone} title={outcome.label}>
          <Text>{outcome.text}</Text>
        </Notice>
      )}
      {clients.state.phase === "loading" ? <Text tone="muted">Looking for clients…</Text> : null}
      {clients.state.phase === "failed" ? <ErrorNotice error={clients.state.error} /> : null}
      {clients.state.phase === "ready" ? (
        <ClientList
          clients={clients.state.data}
          connection={connection}
          workspace={workspace}
          onChanged={changed}
        />
      ) : null}
    </Panel>
  );
}

interface WorkspaceChoiceProps {
  readonly names: readonly string[];
  readonly value: string;
  readonly onChange: (workspace: string) => void;
}

/** What Connect does, and which workspace the tokens it makes may read. */
function WorkspaceChoice({ names, value, onChange }: WorkspaceChoiceProps): ReactElement {
  return (
    <>
      <Text>
        Connect writes the server entry named memini into a client’s own configuration, with a read
        token made for that client. Disconnect removes the entry and revokes the token. Nothing
        else in the file is changed, and the file is copied beside itself before the first change.
      </Text>
      <Field label="What a connected client may read" hint="The token made on Connect is limited to it.">
        <Select
          value={value}
          onChange={onChange}
          options={[EVERY_WORKSPACE, ...names.map((name) => ({ value: name, label: name }))]}
        />
      </Field>
    </>
  );
}

interface ClientListProps {
  readonly clients: readonly AgentClient[];
  readonly connection: Connection;
  readonly workspace: string;
  readonly onChanged: (outcome: ClientSummary | null) => void;
}

function ClientList({ clients, connection, workspace, onChanged }: ClientListProps): ReactElement {
  if (clients.length === 0) {
    return (
      <EmptyState title="No agent client was found on this computer">
        <Text>
          The app looks for Claude Code, Codex CLI, Cursor, VS Code with GitHub Copilot, Gemini CLI,
          Windsurf, Zed, Cline and OpenCode. For another client, or one on another computer, use
          the configuration below.
        </Text>
      </EmptyState>
    );
  }
  const address = mcpAddressOf(connection.url);
  return (
    <>
      {clients.map((client) => (
        <AgentClientCard
          key={client.id}
          client={client}
          connection={connection.id}
          address={address}
          workspace={workspace}
          onChanged={onChanged}
        />
      ))}
    </>
  );
}
