import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Card,
  ErrorNotice,
  Field,
  Form,
  Notice,
  Section,
  Text,
  TextArea,
  TextInput,
} from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { displayText } from "../text/display-text";
import { saveProject } from "./catalog";
import type { Project, ProjectPlace } from "./catalog";
import { EntryEditor } from "./entry-editor";
import { addDomain, addSubdomain, checkProject, sameProject, tidyProject } from "./project-edit";
import { isVersionConflict, parseSaveReport } from "./save-report";
import type { SaveReport } from "./save-report";

export interface ProjectEditorProps {
  readonly place: ProjectPlace;
  /** The project as it was read: the draft starts from it and is saved against its version. */
  readonly project: Project;
  readonly onSaved: (report: SaveReport) => void;
  readonly onCancel: () => void;
  /** Reads the catalog again and drops the draft, after someone else changed the project. */
  readonly onReload: () => void;
}

/**
 * Edits a project: its title and description, its domains and subdomains and their path masks.
 * Saving writes the whole project with the version it was read at, so a change someone else
 * made meanwhile is refused by the server and never overwritten.
 */
export function ProjectEditor(props: ProjectEditorProps): ReactElement {
  const { place, project, onSaved } = props;
  const backend = useBackend();
  const [draft, setDraft] = useState(project);
  const save = useCallback(async (): Promise<void> => {
    onSaved(parseSaveReport(await saveProject(backend, place, tidyProject(draft))));
  }, [backend, place, draft, onSaved]);
  const saving = useAction(save);
  const problem = checkProject(draft);
  const idle = problem !== null || saving.pending || sameProject(draft, project);
  return (
    <Form onSubmit={() => saving.run()}>
      <Field label="Title">
        <TextInput value={draft.title} onChange={(title) => setDraft({ ...draft, title })} maxLength={200} />
      </Field>
      <Field label="Description">
        <TextArea
          value={draft.description}
          onChange={(description) => setDraft({ ...draft, description })}
          rows={3}
        />
      </Field>
      <DomainEditors draft={draft} onDraft={setDraft} />
      {problem === null ? null : <Text tone="failure">{problem}</Text>}
      {isVersionConflict(saving.error) ? (
        <Conflict onReload={props.onReload} />
      ) : (
        <ErrorNotice error={saving.error} />
      )}
      <Actions>
        <Button variant="primary" submit disabled={idle}>
          {saving.pending ? "Saving…" : "Save the project"}
        </Button>
        <Button disabled={saving.pending} onClick={props.onCancel}>
          Cancel
        </Button>
      </Actions>
    </Form>
  );
}

interface ConflictProps {
  readonly onReload: () => void;
}

function Conflict({ onReload }: ConflictProps): ReactElement {
  return (
    <Notice tone="warn" title="Someone changed this project after you opened it">
      <Text>
        Nothing was saved, and the newer version on the server is untouched. Load it to go on from
        there: what you changed here is dropped, so copy what you need first.
      </Text>
      <Actions>
        <Button onClick={onReload}>Load the newer version</Button>
      </Actions>
    </Notice>
  );
}

interface DomainEditorsProps {
  readonly draft: Project;
  readonly onDraft: (draft: Project) => void;
}

function DomainEditors({ draft, onDraft }: DomainEditorsProps): ReactElement {
  return (
    <Section title="Domains">
      {draft.domains.map((domain, at) => (
        <Card key={String(at)} title={`Domain ${displayText(domain.name)}`}>
          <EntryEditor draft={draft} place={{ domain: at, subdomain: null }} onDraft={onDraft} />
          {domain.subdomains.map((subdomain, subAt) => (
            <Card key={String(subAt)} title={`Subdomain ${displayText(subdomain.name)}`}>
              <EntryEditor draft={draft} place={{ domain: at, subdomain: subAt }} onDraft={onDraft} />
            </Card>
          ))}
          <Actions>
            <Button onClick={() => onDraft(addSubdomain(draft, at))}>Add a subdomain</Button>
          </Actions>
        </Card>
      ))}
      <Actions>
        <Button onClick={() => onDraft(addDomain(draft))}>Add a domain</Button>
      </Actions>
    </Section>
  );
}
