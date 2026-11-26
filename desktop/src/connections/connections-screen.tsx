import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Button,
  EmptyState,
  ErrorNotice,
  Mono,
  Panel,
  Pill,
  RowList,
  Screen,
  SelectableRow,
  Text,
} from "../components";
import { useAction } from "../hooks/use-action";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { ConnectionChecker } from "./connection-checker";
import { ConnectionEditor } from "./connection-editor";
import { ConnectionToken } from "./connection-token";
import { formOf, localConnection, newForm } from "./connections";
import type { Connection, ConnectionForm, ConnectionKind, ConnectionsView } from "./connections";
import { activateConnection } from "./connections-api";

export interface ConnectionsScreenProps {
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
}

/** What the lower part of the screen shows: a saved connection, or the form for a new one. */
type Selection =
  | { readonly mode: "saved"; readonly id: string }
  | { readonly mode: "new"; readonly kind: ConnectionKind }
  | null;

export function ConnectionsScreen({ view, info, onView }: ConnectionsScreenProps): ReactElement {
  const [selection, setSelection] = useState<Selection>(
    view.activeId === null ? null : { mode: "saved", id: view.activeId },
  );
  const add = (kind: ConnectionKind): void => {
    setSelection({ mode: "new", kind });
  };
  return (
    <Screen
      title="Connection"
      lead="The servers this app talks to: one on this computer, or others by address and token."
    >
      <Panel title="Saved connections" actions={<AddButtons view={view} onAdd={add} />}>
        <ConnectionList view={view} selection={selection} onSelect={setSelection} onView={onView} />
      </Panel>
      <SelectionPanels
        view={view}
        info={info}
        selection={selection}
        onSelect={setSelection}
        onView={onView}
      />
    </Screen>
  );
}

interface AddButtonsProps {
  readonly view: ConnectionsView;
  readonly onAdd: (kind: ConnectionKind) => void;
}

function AddButtons({ view, onAdd }: AddButtonsProps): ReactElement {
  return (
    <>
      {localConnection(view) === null ? (
        <Button
          onClick={() => {
            onAdd("local");
          }}
        >
          Add this computer
        </Button>
      ) : null}
      <Button
        onClick={() => {
          onAdd("remote");
        }}
      >
        Add a remote server
      </Button>
    </>
  );
}

interface ConnectionListProps {
  readonly view: ConnectionsView;
  readonly selection: Selection;
  readonly onSelect: (selection: Selection) => void;
  readonly onView: (view: ConnectionsView) => void;
}

function ConnectionList(props: ConnectionListProps): ReactElement {
  const { view, selection, onSelect, onView } = props;
  const backend = useBackend();
  const activate = useCallback(
    async (id: string): Promise<void> => {
      onView(await activateConnection(backend, id));
    },
    [backend, onView],
  );
  const action = useAction(activate);
  if (view.connections.length === 0) {
    return (
      <EmptyState title="No connection yet">
        <Text>Add this computer to manage a local server, or a remote server by its address.</Text>
      </EmptyState>
    );
  }
  return (
    <>
      <ErrorNotice error={action.error} />
      <RowList>
        {view.connections.map((connection) => (
          <ConnectionRow
            key={connection.id}
            connection={connection}
            active={connection.id === view.activeId}
            selected={selection?.mode === "saved" && selection.id === connection.id}
            onSelect={() => {
              onSelect({ mode: "saved", id: connection.id });
            }}
            using={action.running?.argument === connection.id}
            blocked={action.pending}
            onUse={() => action.run(connection.id)}
          />
        ))}
      </RowList>
    </>
  );
}

interface ConnectionRowProps {
  readonly connection: Connection;
  readonly active: boolean;
  readonly selected: boolean;
  readonly onSelect: () => void;
  /** It is being made the active connection; another one is. */
  readonly using: boolean;
  readonly blocked: boolean;
  readonly onUse: () => Promise<void>;
}

function ConnectionRow(props: ConnectionRowProps): ReactElement {
  const { connection, active, selected, onSelect, using, blocked, onUse } = props;
  return (
    <SelectableRow
      selected={selected}
      onSelect={onSelect}
      title={connection.name}
      detail={<Mono>{connection.url}</Mono>}
    >
      <Pill tone="info">{connection.kind === "local" ? "this computer" : "remote"}</Pill>
      <Pill tone={connection.hasToken ? "good" : "warn"}>
        {connection.hasToken ? "token stored" : "no token"}
      </Pill>
      {active ? <Pill tone="good">active</Pill> : (
        <Button disabled={blocked} busy={using ? "Switching…" : null} onClick={onUse}>
          Use
        </Button>
      )}
    </SelectableRow>
  );
}

interface SelectionPanelsProps {
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly selection: Selection;
  readonly onSelect: (selection: Selection) => void;
  readonly onView: (view: ConnectionsView) => void;
}

function SelectionPanels(props: SelectionPanelsProps): ReactElement | null {
  const { view, info, selection, onSelect, onView } = props;
  // Counts the clicks on the editor's Test button: a new number shows the test anew.
  const [round, setRound] = useState(0);
  if (selection === null) {
    return null;
  }
  const saved =
    selection.mode === "saved"
      ? (view.connections.find((connection) => connection.id === selection.id) ?? null)
      : null;
  if (selection.mode === "saved" && saved === null) {
    return null;
  }
  const initial: ConnectionForm =
    saved === null
      ? newForm(selection.mode === "new" ? selection.kind : "remote", info.defaultLocalUrl)
      : formOf(saved);
  const editorKey = saved === null ? `new:${initial.kind}` : `${saved.id}:${saved.url}`;
  return (
    <>
      <ConnectionEditor
        key={editorKey}
        initial={initial}
        saved={saved}
        onView={onView}
        onSelect={(id) => {
          onSelect(id === null ? null : { mode: "saved", id });
        }}
        onTest={() => {
          setRound(round + 1);
        }}
      />
      {saved === null ? null : (
        <>
          <ConnectionToken key={`token:${saved.id}`} connection={saved} onView={onView} />
          <ConnectionChecker
            key={`check:${saved.id}:${saved.url}:${String(saved.hasToken)}:${String(round)}`}
            connection={saved}
          />
        </>
      )}
    </>
  );
}
