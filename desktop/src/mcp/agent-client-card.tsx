import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Card,
  Checkbox,
  CodeBlock,
  ConfirmButton,
  ErrorNotice,
  Field,
  Notice,
  Pill,
  Select,
  Text,
} from "../components";
import { useAction } from "../hooks/use-action";
import type { Action } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import {
  connectClient,
  connectNote,
  connectProblem,
  describeClient,
  describeDisconnected,
  disconnectClient,
  literalQuestion,
  loadSnippets,
  needsVariant,
  writesLiteral,
} from "./agent-clients";
import type { AgentClient, ClientSummary, ConnectChoice } from "./agent-clients";

export interface AgentClientCardProps {
  readonly client: AgentClient;
  /** The saved connection Connect points the client at. */
  readonly connection: string;
  /** The MCP address of that connection, to tell an entry for another server. */
  readonly address: string | null;
  /** The workspace the client's token may read; empty: every workspace. */
  readonly workspace: string;
  /** Called after Connect or Disconnect, with what to tell the user. */
  readonly onChanged: (outcome: ClientSummary | null) => void;
}

/** One agent client: what its file says, and Connect or Disconnect on a click. */
export function AgentClientCard(props: AgentClientCardProps): ReactElement {
  const { client } = props;
  const summary = describeClient(client, props.address);
  const connected = client.status.state === "connected";
  return (
    <Card title={client.name} subtitle={client.file}>
      <Text>
        <Pill tone={summary.tone}>{summary.label}</Pill> {summary.text}
      </Text>
      {client.status.state === "absent" ? <ConnectControls {...props} /> : null}
      {connected ? <ConnectedControls {...props} /> : null}
      {!connected && client.hasToken ? <LeftoverToken {...props} /> : null}
    </Card>
  );
}

function ConnectControls(props: AgentClientCardProps): ReactElement {
  const { client, connection, workspace, onChanged } = props;
  const backend = useBackend();
  const [literal, setLiteral] = useState(false);
  const [variant, setVariant] = useState("");
  const choice: ConnectChoice = { workspace, literal, variant };
  const connect = useCallback(
    async (chosen: ConnectChoice): Promise<void> => {
      await connectClient(backend, { client, connection }, chosen);
      onChanged(null);
    },
    [backend, client, connection, onChanged],
  );
  const connecting = useAction(connect);
  const blocked = connectProblem(client, choice) !== null;
  return (
    <>
      {needsVariant(client) ? <VariantChoice client={client} value={variant} onChange={setVariant} /> : null}
      {client.variable !== null && client.takesLiteral ? (
        <Checkbox checked={literal} onChange={setLiteral}>
          Write the token into the client’s file instead
        </Checkbox>
      ) : null}
      <Text tone="muted" small>
        {connectNote(client, choice)}
      </Text>
      <Actions>
        {writesLiteral(client, choice) ? (
          <ConfirmButton
            label="Connect"
            question={literalQuestion(client)}
            confirmLabel="Write the token"
            working="Connecting…"
            disabled={blocked || connecting.pending}
            onConfirm={() => connecting.run(choice)}
          />
        ) : (
          <Button
            variant="primary"
            disabled={blocked}
            busy={connecting.pending ? "Connecting…" : null}
            onClick={() => connecting.run(choice)}
          >
            Connect
          </Button>
        )}
      </Actions>
      <ErrorNotice error={connecting.error} />
    </>
  );
}

interface VariantChoiceProps {
  readonly client: AgentClient;
  readonly value: string;
  readonly onChange: (variant: string) => void;
}

/** Which layout the client's file has, for a client whose file does not tell. */
function VariantChoice({ client, value, onChange }: VariantChoiceProps): ReactElement {
  return (
    <Field label={`Version of ${client.name}`} hint="Its file does not tell which layout it uses.">
      <Select
        value={value}
        onChange={onChange}
        options={[
          { value: "", label: "Choose…" },
          ...client.variants.map((item) => ({ value: item.id, label: item.label })),
        ]}
      />
    </Field>
  );
}

/** Disconnect for the client of the card, also used to revoke a token that was left valid. */
function useDisconnect(props: AgentClientCardProps): Action<void> {
  const { client, onChanged } = props;
  const backend = useBackend();
  const disconnect = useCallback(async (): Promise<void> => {
    onChanged(describeDisconnected(await disconnectClient(backend, client.id)));
  }, [backend, client.id, onChanged]);
  return useAction(disconnect);
}

function ConnectedControls(props: AgentClientCardProps): ReactElement {
  const { client } = props;
  const disconnecting = useDisconnect(props);
  const snippets = client.status.state === "connected" && client.status.token === "reference" ? loadSnippets(client) : [];
  return (
    <>
      {snippets.length === 0 ? null : (
        <Text tone="muted" small>
          Load the token in the shell that starts {client.name}, for example from your shell’s
          profile:
        </Text>
      )}
      {snippets.map((snippet) => (
        <CodeBlock key={snippet.label} label={snippet.label} text={snippet.text} wrap />
      ))}
      <Actions>
        <ConfirmButton
          label="Disconnect"
          question={`Remove the memini entry from the file of ${client.name}${client.hasToken ? " and revoke its token" : ""}?`}
          confirmLabel="Disconnect"
          working="Disconnecting…"
          disabled={disconnecting.pending}
          onConfirm={() => disconnecting.run()}
        />
      </Actions>
      <ErrorNotice error={disconnecting.error} />
    </>
  );
}

/** The client has no entry, but a token the app made for it is still valid on the server. */
function LeftoverToken(props: AgentClientCardProps): ReactElement {
  const disconnecting = useDisconnect(props);
  return (
    <Notice tone="warn" title="A token made for this client is still valid">
      <Text>The entry is gone, but its token was not revoked. Revoke it when the server answers.</Text>
      <Actions>
        <Button busy={disconnecting.pending ? "Revoking…" : null} onClick={() => disconnecting.run()}>
          Revoke the token
        </Button>
      </Actions>
      <ErrorNotice error={disconnecting.error} />
    </Notice>
  );
}
