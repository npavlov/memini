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
  Select,
  Text,
  TextInput,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { ConnectionRebuilds } from "../status/index-rebuilds";
import { AddRepository } from "./add-repository";
import { LocalFolders } from "./local-folders";
import { isHandle } from "./repositories";
import { createWorkspace, listWorkspaces } from "./repositories-api";
import { CodebaseList } from "./codebase-list";

export interface RepositoriesScreenProps {
  readonly connection: Connection;
  readonly info: AppInfo;
}

/**
 * The code bases of a workspace (S17, S18): add a folder of this computer, given to the local
 * server by the launcher, or a remote by its address; choose what each indexes; see how far each
 * sync is. Needs a token with the admin scope.
 */
export function RepositoriesScreen({ connection, info }: RepositoriesScreenProps): ReactElement {
  return (
    <Screen
      title="Codebases"
      lead="The code the server follows and indexes: add a folder of this computer or a repository at an address, choose what it indexes, and see how far each sync is. Needs a token with the admin scope."
    >
      {connection.hasToken ? (
        <>
          <ConnectionRebuilds connectionId={connection.id} />
          <Workspaces connection={connection} info={info} />
        </>
      ) : (
        <Notice tone="warn" title="This connection has no token">
          <Text>Store a token with the admin scope on the Connection screen.</Text>
        </Notice>
      )}
    </Screen>
  );
}

function Workspaces({ connection, info }: RepositoriesScreenProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(useCallback(() => listWorkspaces(backend, connection.id), [backend, connection.id]));
  const [chosen, setChosen] = useState<string | null>(null);
  // The form to add a repository opens on a click and closes once it has added one.
  const [adding, setAdding] = useState(false);
  // Bumped after a change, so the lists below read the server again.
  const [round, setRound] = useState(0);
  const changed = useCallback((): void => {
    setRound((value) => value + 1);
  }, []);
  const added = useCallback((): void => {
    setAdding(false);
    changed();
  }, [changed]);
  if (loaded.state.phase === "loading") {
    return <Text tone="muted">Asking the server…</Text>;
  }
  if (loaded.state.phase === "failed") {
    return <ErrorNotice error={loaded.state.error} />;
  }
  const names = loaded.state.data;
  const workspace = chosen !== null && names.includes(chosen) ? chosen : (names[0] ?? null);
  const local = connection.kind === "local";
  return (
    <>
      <Panel title="Workspace">
        {names.length === 0 ? (
          <Text>A code base goes into a workspace. This server has none yet: create one.</Text>
        ) : (
          <Field label="Code bases of" hint="Each workspace has its own code bases and its own project map.">
            <Select
              value={workspace ?? ""}
              options={names.map((name) => ({ value: name, label: name }))}
              onChange={setChosen}
            />
          </Field>
        )}
        <NewWorkspace
          connection={connection}
          startOpen={names.length === 0}
          onCreated={(name) => {
            setChosen(name);
            loaded.reload();
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
                Add a code base
              </Button>
            </Actions>
          )}
          <CodebaseList key={`${workspace}:${String(round)}`} connection={connection} workspace={workspace} />
        </>
      )}
      {local ? <LocalFolders key={String(round)} onChanged={changed} /> : null}
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
        <Field label="New workspace" note="optional" grow>
          <TextInput value={name} kind="code" maxLength={63} placeholder="harbour" onChange={setName} />
        </Field>
      </Columns>
      {name === "" || valid ? null : (
        <Text tone="hint">Lowercase letters, digits, '-' or '_', starting with a letter or digit.</Text>
      )}
      <ErrorNotice error={creating.error} />
      <Actions>
        <Button submit disabled={!valid || creating.pending}>
          {creating.pending ? "Creating…" : "Create workspace"}
        </Button>
        <Button onClick={onCancel}>Cancel</Button>
      </Actions>
    </Form>
  );
}
