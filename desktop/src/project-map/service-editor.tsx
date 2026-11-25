import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Card,
  ConfirmButton,
  Field,
  FieldGrid,
  Section,
  Select,
  Text,
  TextArea,
  TextInput,
} from "../components";
import { displayText } from "../text/display-text";
import type { Project } from "./catalog";
import { addService, editService, removalQuestion, removeService, repositoryChoices } from "./service-edit";
import type { ServiceChange } from "./service-edit";

export interface ServiceEditorsProps {
  /** The whole draft: an edit of one service gives a new draft. */
  readonly draft: Project;
  /** The git sources of the workspace: a service is a repository of them, or a directory of one. */
  readonly repositories: readonly string[];
  readonly onDraft: (draft: Project) => void;
}

/** The services of a project in its editor: each with its name, repository, directory and description. */
export function ServiceEditors({ draft, repositories, onDraft }: ServiceEditorsProps): ReactElement {
  const first = repositories[0];
  return (
    <Section title="Services">
      {draft.services.map((service, at) => (
        <Card key={String(at)} title={`Service ${displayText(service.name)}`}>
          <ServiceFields draft={draft} at={at} repositories={repositories} onDraft={onDraft} />
        </Card>
      ))}
      {first === undefined ? (
        <Text tone="muted">
          This workspace has no git source yet, and a service is a repository or a directory of one.
          Add a repository to the workspace first.
        </Text>
      ) : (
        <Actions>
          <Button onClick={() => onDraft(addService(draft, first))}>Add a service</Button>
        </Actions>
      )}
    </Section>
  );
}

interface ServiceFieldsProps extends ServiceEditorsProps {
  /** Which service of the draft. */
  readonly at: number;
}

function ServiceFields({ draft, at, repositories, onDraft }: ServiceFieldsProps): ReactElement | null {
  const service = draft.services[at];
  if (service === undefined) {
    return null;
  }
  const change = (changed: ServiceChange): void => {
    onDraft(editService(draft, at, changed));
  };
  const choices = repositoryChoices(repositories, service.repository);
  return (
    <>
      <FieldGrid>
        <Field label="Name" hint="Lowercase letters, digits, - or _. To another project that names it, a renamed service is gone.">
          <TextInput kind="code" value={service.name} onChange={(name) => change({ name })} maxLength={63} />
        </Field>
        <Field label="Repository">
          <Select
            value={service.repository}
            onChange={(repository) => change({ repository })}
            options={choices.map((name) => ({ value: name, label: name }))}
          />
        </Field>
        <Field label="Directory" hint="Inside the repository; empty for the whole of it.">
          <TextInput kind="code" value={service.root} onChange={(root) => change({ root })} placeholder="services/billing" />
        </Field>
      </FieldGrid>
      <Field label="Description">
        <TextArea value={service.description} onChange={(description) => change({ description })} rows={2} />
      </Field>
      {service.origin === null ? (
        <Text tone="muted" small>
          A domain can name this service once the project is saved with it.
        </Text>
      ) : null}
      <Actions>
        <ConfirmButton
          label="Remove the service"
          question={removalQuestion(draft, at)}
          confirmLabel="Remove"
          onConfirm={async () => {
            onDraft(removeService(draft, at));
          }}
        />
      </Actions>
    </>
  );
}
