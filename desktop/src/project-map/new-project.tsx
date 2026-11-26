import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  ErrorNotice,
  Field,
  FieldGrid,
  Form,
  Notice,
  Panel,
  Text,
  TextArea,
  TextInput,
} from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { newProject, saveProject } from "./catalog";
import type { NewProject, ProjectPlace } from "./catalog";
import { NAME_RULE, isName } from "./names";
import { isNameTaken } from "./save-refusal";

export interface NewProjectPanelProps {
  /** The workspace the project is created in. */
  readonly place: ProjectPlace;
  /** Called with the name of the project once the server has made it. */
  readonly onCreated: (name: string) => void;
}

const NOTHING: NewProject = { name: "", title: "", description: "" };

/**
 * Creates a project: a name, a title and a description. It starts empty. The server is told
 * that no project of that name may exist yet, so one that does is never overwritten.
 */
export function NewProjectPanel({ place, onCreated }: NewProjectPanelProps): ReactElement {
  const backend = useBackend();
  const [open, setOpen] = useState(false);
  const [entered, setEntered] = useState(NOTHING);
  const create = useCallback(async (): Promise<void> => {
    const project = newProject(entered);
    await saveProject(backend, place, project);
    setEntered(NOTHING);
    setOpen(false);
    onCreated(project.name);
  }, [backend, place, entered, onCreated]);
  const creating = useAction(create);
  if (!open) {
    return (
      <Actions>
        <Button onClick={() => setOpen(true)}>New project</Button>
      </Actions>
    );
  }
  const named = isName(entered.name);
  return (
    <Panel title="New project">
      <Form onSubmit={() => (named ? creating.run() : undefined)}>
        <NewProjectFields entered={entered} onChange={setEntered} />
        <Text tone="muted" small>
          It starts empty, in the workspace {place.workspace}: its services and domains are added in
          the map.
        </Text>
        {entered.name === "" || named ? null : <Text tone="hint">A project needs a name of {NAME_RULE}.</Text>}
        {isNameTaken(creating.error) ? <NameTaken /> : <ErrorNotice error={creating.error} />}
        <Actions>
          <Button variant="primary" submit disabled={!named} busy={creating.pending ? "Creating…" : null}>
            Create the project
          </Button>
          <Button disabled={creating.pending} onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </Actions>
      </Form>
    </Panel>
  );
}

function NameTaken(): ReactElement {
  return (
    <Notice tone="bad" title="A project of that name exists">
      <Text>
        Nothing was written, and that project is untouched. Choose another name, or edit the
        existing project in the map.
      </Text>
    </Notice>
  );
}

interface NewProjectFieldsProps {
  readonly entered: NewProject;
  readonly onChange: (entered: NewProject) => void;
}

function NewProjectFields({ entered, onChange }: NewProjectFieldsProps): ReactElement {
  return (
    <>
      <FieldGrid>
        <Field label="Name" hint="Unique in the workspace. Search and decisions refer to it; it is not changed later.">
          <TextInput
            kind="code"
            value={entered.name}
            onChange={(name) => onChange({ ...entered, name })}
            maxLength={63}
            placeholder="port"
          />
        </Field>
        <Field label="Title">
          <TextInput
            value={entered.title}
            onChange={(title) => onChange({ ...entered, title })}
            maxLength={200}
            placeholder="Port operations"
          />
        </Field>
      </FieldGrid>
      <Field label="Description">
        <TextArea
          value={entered.description}
          onChange={(description) => onChange({ ...entered, description })}
          rows={3}
        />
      </Field>
    </>
  );
}
