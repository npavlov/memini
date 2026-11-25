import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { ErrorNotice, Notice } from "../ui/notice";
import { Panel, Screen } from "../ui/panel";
import { Pill } from "../ui/pill";
import { useAction } from "../ui/use-action";
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

/** What the right side of the screen shows: a saved connection, or the form for a new one. */
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
        <button
          type="button"
          className="button small"
          onClick={() => {
            onAdd("local");
          }}
        >
          Add this computer
        </button>
      ) : null}
      <button
        type="button"
        className="button small"
        onClick={() => {
          onAdd("remote");
        }}
      >
        Add a remote server
      </button>
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
      <Notice tone="info" title="No connection yet">
        <p>Add this computer to manage a local server, or a remote server by its address.</p>
      </Notice>
    );
  }
  return (
    <>
      <ErrorNotice error={action.error} />
      <ul className="rows">
        {view.connections.map((connection) => (
          <ConnectionRow
            key={connection.id}
            connection={connection}
            active={connection.id === view.activeId}
            selected={selection?.mode === "saved" && selection.id === connection.id}
            onSelect={() => {
              onSelect({ mode: "saved", id: connection.id });
            }}
            onUse={() => action.run(connection.id)}
          />
        ))}
      </ul>
    </>
  );
}

interface ConnectionRowProps {
  readonly connection: Connection;
  readonly active: boolean;
  readonly selected: boolean;
  readonly onSelect: () => void;
  readonly onUse: () => Promise<void>;
}

function ConnectionRow(props: ConnectionRowProps): ReactElement {
  const { connection, active, selected, onSelect, onUse } = props;
  return (
    <li className={selected ? "row selected" : "row"}>
      <button type="button" className="row-main" onClick={onSelect}>
        <strong>{connection.name}</strong>
        <span className="muted">{connection.url}</span>
      </button>
      <Pill tone="info">{connection.kind === "local" ? "this computer" : "remote"}</Pill>
      <Pill tone={connection.hasToken ? "good" : "warn"}>
        {connection.hasToken ? "token stored" : "no token"}
      </Pill>
      {active ? (
        <Pill tone="good">active</Pill>
      ) : (
        <button type="button" className="button small" onClick={onUse}>
          Use
        </button>
      )}
    </li>
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
      />
      {saved === null ? null : (
        <>
          <ConnectionToken key={`token:${saved.id}`} connection={saved} onView={onView} />
          <ConnectionChecker key={`check:${saved.id}:${saved.url}`} connection={saved} />
        </>
      )}
    </>
  );
}
