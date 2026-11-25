import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { useBackend } from "../ipc/backend-context";
import { ErrorNotice } from "../ui/notice";
import { Panel } from "../ui/panel";
import { useAction } from "../ui/use-action";
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
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          return problem === null ? creating.run() : undefined;
        }}
      >
        <label className="field">
          <span>What it is for</span>
          <input
            type="text"
            value={form.name}
            maxLength={200}
            placeholder="laptop agent"
            onChange={(event) => {
              setForm({ ...form, name: event.target.value });
            }}
          />
        </label>
        <ScopeChoices form={form} onChange={setForm} />
        <LimitChoices form={form} workspaces={workspaces} onChange={setForm} />
        {form.name === "" || problem === null ? null : <p className="hint">{problem}</p>}
        <ErrorNotice error={creating.error} />
        <div className="actions">
          <button type="submit" className="button primary" disabled={problem !== null || creating.pending}>
            {creating.pending ? "Creating…" : "Create token"}
          </button>
        </div>
      </form>
    </Panel>
  );
}

interface ChoicesProps {
  readonly form: NewTokenForm;
  readonly onChange: (form: NewTokenForm) => void;
}

function ScopeChoices({ form, onChange }: ChoicesProps): ReactElement {
  return (
    <fieldset className="choices">
      <legend>What it may do</legend>
      {SCOPES.map((scope) => (
        <label key={scope} className="check">
          <input
            type="checkbox"
            checked={form.scopes.includes(scope)}
            onChange={() => {
              onChange({ ...form, scopes: toggleScope(form.scopes, scope) });
            }}
          />
          <span>
            <strong>{scope}</strong> <span className="muted">{SCOPE_MEANINGS[scope]}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

interface LimitChoicesProps extends ChoicesProps {
  readonly workspaces: readonly string[];
}

function LimitChoices({ form, workspaces, onChange }: LimitChoicesProps): ReactElement {
  return (
    <div className="columns">
      <label className="field">
        <span>Workspace</span>
        <select
          value={form.workspace}
          onChange={(event) => {
            onChange({ ...form, workspace: event.target.value });
          }}
        >
          <option value="">Every workspace</option>
          {workspaces.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
      </label>
      <label className="field">
        <span>Expires</span>
        <select
          value={form.expiry}
          onChange={(event) => {
            onChange({ ...form, expiry: readExpiry(event.target.value) });
          }}
        >
          {EXPIRIES.map((expiry) => (
            <option key={expiry.id} value={expiry.id}>
              {expiry.label}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
