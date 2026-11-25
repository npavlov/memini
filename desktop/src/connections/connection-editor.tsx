import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Checkbox,
  ConfirmButton,
  ErrorNotice,
  Field,
  Form,
  Notice,
  Panel,
  Text,
  TextInput,
} from "../components";
import { useAction } from "../hooks/use-action";
import type { Action } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { checkConnectionForm, dropsToken, isPlainHttpToAnotherComputer } from "./connections";
import type { Connection, ConnectionForm, ConnectionsView } from "./connections";
import { deleteConnection, saveConnection } from "./connections-api";

export interface ConnectionEditorProps {
  readonly initial: ConnectionForm;
  /** The saved connection being changed; null for a new one. */
  readonly saved: Connection | null;
  readonly onView: (view: ConnectionsView) => void;
  /** Shows a saved connection after a save, or nothing after a delete or a cancel. */
  readonly onSelect: (id: string | null) => void;
}

function titleOf(form: ConnectionForm, saved: Connection | null): string {
  if (saved !== null) {
    return `Connection: ${saved.name}`;
  }
  return form.kind === "local" ? "New connection: this computer" : "New connection: remote server";
}

export function ConnectionEditor(props: ConnectionEditorProps): ReactElement {
  const { initial, saved, onView, onSelect } = props;
  const backend = useBackend();
  const [form, setForm] = useState(initial);
  const problem = checkConnectionForm(form);
  const save = useCallback(async (): Promise<void> => {
    const view = await saveConnection(backend, form);
    onView(view);
    // A new connection is appended to the list.
    onSelect(form.id ?? view.connections.at(-1)?.id ?? null);
  }, [backend, form, onView, onSelect]);
  const remove = useCallback(async (): Promise<void> => {
    if (saved !== null) {
      onView(await deleteConnection(backend, saved.id));
      onSelect(null);
    }
  }, [backend, saved, onView, onSelect]);
  const saving = useAction(save);
  const removing = useAction(remove);
  return (
    <Panel title={titleOf(form, saved)}>
      <Form onSubmit={() => (problem === null ? saving.run() : undefined)}>
        <ConnectionFields form={form} onChange={setForm} />
        {problem === null ? null : <Text tone="hint">{problem}</Text>}
        {dropsToken(form, saved) ? <TokenWillBeRemoved /> : null}
        <ErrorNotice error={saving.error ?? removing.error} />
        <EditorButtons
          saved={saved}
          blocked={problem !== null}
          saving={saving}
          removing={removing}
          onCancel={() => {
            onSelect(null);
          }}
        />
      </Form>
    </Panel>
  );
}

function TokenWillBeRemoved(): ReactElement {
  return (
    <Notice tone="warn">
      <Text>
        This address is another server. A token belongs to the server it was stored for, so saving
        removes this connection’s token.
      </Text>
    </Notice>
  );
}

interface EditorButtonsProps {
  readonly saved: Connection | null;
  /** The form has a problem, so it cannot be saved. */
  readonly blocked: boolean;
  readonly saving: Action<void>;
  readonly removing: Action<void>;
  readonly onCancel: () => void;
}

function EditorButtons(props: EditorButtonsProps): ReactElement {
  const { saved, blocked, saving, removing, onCancel } = props;
  return (
    <Actions>
      <Button variant="primary" submit disabled={blocked || saving.pending}>
        {saved === null ? "Save connection" : "Save changes"}
      </Button>
      {saved === null ? (
        <Button onClick={onCancel}>Cancel</Button>
      ) : (
        <ConfirmButton
          label="Delete connection"
          question="Delete this connection and its token?"
          confirmLabel="Delete"
          disabled={removing.pending}
          onConfirm={() => removing.run()}
        />
      )}
    </Actions>
  );
}

interface FieldsProps {
  readonly form: ConnectionForm;
  readonly onChange: (form: ConnectionForm) => void;
}

const LOCAL_ADDRESS = "Where the server on this computer answers. The Server screen shows the launcher’s address.";
const REMOTE_ADDRESS = "The address the server’s operator gave you. Use https:// for another computer.";

function ConnectionFields({ form, onChange }: FieldsProps): ReactElement {
  const local = form.kind === "local";
  return (
    <>
      <Field label="Name">
        <TextInput
          value={form.name}
          maxLength={80}
          placeholder={local ? "This computer" : "Harbour team"}
          onChange={(name) => {
            onChange({ ...form, name });
          }}
        />
      </Field>
      <Field label="Address" hint={local ? LOCAL_ADDRESS : REMOTE_ADDRESS}>
        <TextInput
          kind="code"
          value={form.url}
          placeholder="https://memini.example.org"
          onChange={(url) => {
            onChange({ ...form, url });
          }}
        />
      </Field>
      {!local && isPlainHttpToAnotherComputer(form.url) ? (
        <Checkbox
          checked={form.allowHttp}
          onChange={(allowHttp) => {
            onChange({ ...form, allowHttp });
          }}
        >
          Allow plain HTTP for this connection. The token can be read on the network.
        </Checkbox>
      ) : null}
    </>
  );
}
