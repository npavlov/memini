import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Columns,
  EmptyState,
  ErrorNotice,
  Field,
  Form,
  Notice,
  Panel,
  Screen,
  Text,
  TextInput,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { ConnectionRebuilds } from "../status/index-rebuilds";
import { listTokens } from "../tokens/tokens-api";
import type { WorkspaceChoice } from "../workspaces/use-workspaces";
import { WORKSPACE_MEANING } from "../workspaces/workspaces";
import { AddRepository } from "./add-repository";
import { CodebaseList } from "./codebase-list";
import { isHandle } from "./repositories";
import type { LocalRepo } from "./repositories";
import { createWorkspace, listFolders } from "./repositories-api";

export interface RepositoriesScreenProps {
  readonly connection: Connection;
  readonly info: AppInfo;
  /** The workspaces, and the one chosen in the sidebar. */
  readonly workspaces: WorkspaceChoice;
}

/**
 * The code bases of the workspace chosen in the sidebar (S17, S18): add a folder of this
 * computer, given to the local server by the launcher, or a remote by its address; choose what
 * each indexes; see what the index holds, the last check, and how much the project map names.
 * Needs a token with the admin scope.
 */
export function RepositoriesScreen({ connection, info, workspaces }: RepositoriesScreenProps): ReactElement {
  const workspace = workspaces.current;
  return (
    <Screen
      title={workspace === null ? "Codebases" : `Codebases of ${workspace}`}
      lead="The code the server follows and indexes for the workspace chosen in the sidebar: add a folder of this computer or a repository at an address, choose what it indexes, and see what the index holds. Needs a token with the admin scope."
    >
      {connection.hasToken ? (
        <>
          <ConnectionRebuilds connectionId={connection.id} />
          <Workspace connection={connection} info={info} workspaces={workspaces} />
        </>
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <Text>Store a token with the admin scope on the Connection screen.</Text>
        </Notice>
      )}
    </Screen>
  );
}

interface Extras {
  /** The folders given to the local server; null for another server, or when the launcher cannot say. */
  readonly folders: readonly LocalRepo[] | null;
  readonly tokenNames: ReadonlyMap<string, string>;
}

/** What the cards need beside the server's sources: the local folders, and the names of tokens. */
async function readExtras(backend: Backend, id: string, local: boolean): Promise<Extras> {
  const folders = local
    ? await listFolders(backend).then(
        (answer) => (answer.ok ? answer.repositories : null),
        () => null,
      )
    : null;
  const tokens = await listTokens(backend, id).catch(() => []);
  return { folders, tokenNames: new Map(tokens.map((token) => [token.id, token.name])) };
}

function Workspace({ connection, info, workspaces }: RepositoriesScreenProps): ReactElement {
  const backend = useBackend();
  const local = connection.kind === "local";
  const extras = useLoad(
    useCallback(() => readExtras(backend, connection.id, local), [backend, connection.id, local]),
  );
  // The form to add a repository opens on a click and closes once it has added one.
  const [adding, setAdding] = useState(false);
  // Bumped after a change, so the list below reads the server again.
  const [round, setRound] = useState(0);
  const { reload: reloadExtras } = extras;
  const added = useCallback((): void => {
    setAdding(false);
    setRound((value) => value + 1);
    reloadExtras();
  }, [reloadExtras]);
  if (workspaces.error !== null) {
    return <ErrorNotice error={workspaces.error} />;
  }
  if (workspaces.names === null) {
    return <Text tone="muted">Asking the server…</Text>;
  }
  const workspace = workspaces.current;
  const known = extras.state.phase === "ready" ? extras.state.data : { folders: null, tokenNames: new Map<string, string>() };
  return (
    <>
      <Panel title={workspace === null ? "Workspace" : `Workspace ${workspace}`}>
        <Text>{WORKSPACE_MEANING}</Text>
        {workspace === null ? (
          <Text>This server has none yet: create one, then add its code bases.</Text>
        ) : (
          <Text tone="muted" small>
            Switch the workspace in the sidebar: every screen shows the one chosen there. Add another
            for a second company or client whose code must not mix with this one.
          </Text>
        )}
        <NewWorkspace
          connection={connection}
          startOpen={workspaces.names.length === 0}
          onCreated={(name) => {
            workspaces.reload(name);
          }}
        />
      </Panel>
      {workspace === null ? (
        <EmptyState title="No workspace yet">
          <Text>Create a workspace above, then add its code bases.</Text>
        </EmptyState>
      ) : (
        <>
          {adding ? (
            <AddRepository
              connection={connection}
              info={info}
              workspace={workspace}
              local={local}
              onAdded={added}
              onCancel={() => {
                setAdding(false);
              }}
            />
          ) : (
            <Actions>
              <Button
                variant="primary"
                onClick={() => {
                  setAdding(true);
                }}
              >
                Add a code base to {workspace}
              </Button>
            </Actions>
          )}
          <CodebaseList
            key={`${workspace}:${String(round)}`}
            connection={connection}
            workspace={workspace}
            folders={known.folders}
            tokenNames={known.tokenNames}
          />
        </>
      )}
    </>
  );
}

interface NewWorkspaceProps {
  readonly connection: Connection;
  /** Open from the start: a server without a workspace needs one first. */
  readonly startOpen?: boolean;
  readonly onCreated: (name: string) => void;
}

function NewWorkspace({ connection, startOpen = false, onCreated }: NewWorkspaceProps): ReactElement {
  const [open, setOpen] = useState(startOpen);
  if (!open) {
    return (
      <Actions>
        <Button
          onClick={() => {
            setOpen(true);
          }}
        >
          New workspace
        </Button>
      </Actions>
    );
  }
  return (
    <NewWorkspaceForm
      connection={connection}
      onCreated={(name) => {
        setOpen(false);
        onCreated(name);
      }}
      onCancel={() => {
        setOpen(false);
      }}
    />
  );
}

interface NewWorkspaceFormProps extends NewWorkspaceProps {
  readonly onCancel: () => void;
}

function NewWorkspaceForm({ connection, onCreated, onCancel }: NewWorkspaceFormProps): ReactElement {
  const backend = useBackend();
  const [name, setName] = useState("");
  const valid = isHandle(name);
  const create = useCallback(async (): Promise<void> => {
    await createWorkspace(backend, connection.id, name);
    setName("");
    onCreated(name);
  }, [backend, connection.id, name, onCreated]);
  const creating = useAction(create);
  return (
    <Form inline onSubmit={() => (valid ? creating.run() : undefined)}>
      <Columns>
        <Field label="New workspace" hint="Its name: lowercase, such as the company's or client's." grow>
          <TextInput value={name} kind="code" maxLength={63} placeholder="harbour" onChange={setName} />
        </Field>
      </Columns>
      {name === "" || valid ? null : (
        <Text tone="hint">Lowercase letters, digits, '-' or '_', starting with a letter or digit.</Text>
      )}
      <ErrorNotice error={creating.error} />
      <Actions>
        <Button submit disabled={!valid} busy={creating.pending ? "Creating…" : null}>
          Create workspace
        </Button>
        <Button disabled={creating.pending} onClick={onCancel}>
          Cancel
        </Button>
      </Actions>
    </Form>
  );
}
