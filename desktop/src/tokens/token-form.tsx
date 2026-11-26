import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Checkbox,
  Columns,
  ErrorNotice,
  Field,
  FieldGroup,
  Form,
  Panel,
  Select,
  Text,
  TextInput,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import {
  EMPTY_TOKEN_FORM,
  EXPIRIES,
  SCOPES,
  SCOPE_MEANINGS,
  checkNewToken,
  toggleScope,
} from "./tokens";
import type { CreatedToken, ExpiryId, NewTokenForm } from "./tokens";
import { createToken } from "./tokens-api";

export interface TokenFormProps {
  readonly connection: Connection;
  readonly workspaces: readonly string[];
  readonly onCreated: (created: CreatedToken) => void;
}

function readExpiry(value: string): ExpiryId {
  return EXPIRIES.find((expiry) => expiry.id === value)?.id ?? "never";
}

export function TokenForm({ connection, workspaces, onCreated }: TokenFormProps): ReactElement {
  const backend = useBackend();
  const [form, setForm] = useState<NewTokenForm>(EMPTY_TOKEN_FORM);
  const problem = checkNewToken(form);
  const create = useCallback(async (): Promise<void> => {
    onCreated(await createToken(backend, connection.id, form));
    setForm(EMPTY_TOKEN_FORM);
  }, [backend, connection.id, form, onCreated]);
  const creating = useAction(create);
  return (
    <Panel title="Create a token">
      <Form onSubmit={() => (problem === null ? creating.run() : undefined)}>
        <Field label="What it is for">
          <TextInput
            value={form.name}
            maxLength={200}
            placeholder="laptop agent"
            onChange={(name) => {
              setForm({ ...form, name });
            }}
          />
        </Field>
        <ScopeChoices form={form} onChange={setForm} />
        <LimitChoices form={form} workspaces={workspaces} onChange={setForm} />
        {form.name === "" || problem === null ? null : <Text tone="hint">{problem}</Text>}
        <ErrorNotice error={creating.error} />
        <Actions>
          <Button variant="primary" submit disabled={problem !== null} busy={creating.pending ? "Creating…" : null}>
            Create token
          </Button>
        </Actions>
      </Form>
    </Panel>
  );
}

interface ChoicesProps {
  readonly form: NewTokenForm;
  readonly onChange: (form: NewTokenForm) => void;
}

function ScopeChoices({ form, onChange }: ChoicesProps): ReactElement {
  return (
    <FieldGroup label="What it may do">
      {SCOPES.map((scope) => (
        <Checkbox
          key={scope}
          checked={form.scopes.includes(scope)}
          onChange={() => {
            onChange({ ...form, scopes: toggleScope(form.scopes, scope) });
          }}
        >
          <strong>{scope}</strong>{" "}
          <Text tone="muted" inline>
            {SCOPE_MEANINGS[scope]}
          </Text>
        </Checkbox>
      ))}
    </FieldGroup>
  );
}

interface LimitChoicesProps extends ChoicesProps {
  readonly workspaces: readonly string[];
}

function LimitChoices({ form, workspaces, onChange }: LimitChoicesProps): ReactElement {
  const everywhere = { value: "", label: "Every workspace" };
  return (
    <Columns>
      <Field label="Workspace">
        <Select
          value={form.workspace}
          options={[everywhere, ...workspaces.map((name) => ({ value: name, label: name }))]}
          onChange={(workspace) => {
            onChange({ ...form, workspace });
          }}
        />
      </Field>
      <Field label="Expires">
        <Select
          value={form.expiry}
          options={EXPIRIES.map((expiry) => ({ value: expiry.id, label: expiry.label }))}
          onChange={(expiry) => {
            onChange({ ...form, expiry: readExpiry(expiry) });
          }}
        />
      </Field>
    </Columns>
  );
}
