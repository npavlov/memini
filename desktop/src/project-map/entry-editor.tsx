import { useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, ConfirmButton, Field, FieldGrid, Select, TextArea, TextInput } from "../components";
import type { Project, Subdomain } from "./catalog";
import { addCode, editEntry, entryAt, removeEntry, setMasks, unattachedServices } from "./project-edit";
import type { EntryPlace } from "./project-edit";
import { shownService } from "./service-edit";
import type { NamedService } from "./service-edit";

export interface EntryEditorProps {
  /** The whole draft: an edit of one entry gives a new draft. */
  readonly draft: Project;
  readonly place: EntryPlace;
  readonly onDraft: (draft: Project) => void;
}

/** The fields of one domain or subdomain: its name, title, description and path masks. */
export function EntryEditor({ draft, place, onDraft }: EntryEditorProps): ReactElement | null {
  const entry = entryAt(draft, place);
  if (entry === undefined) {
    return null;
  }
  const change = (changed: Partial<Subdomain>): void => {
    onDraft(editEntry(draft, place, changed));
  };
  const what = place.subdomain === null ? "domain" : "subdomain";
  return (
    <>
      <FieldGrid>
        <Field label="Name" hint="Lowercase letters, digits, - or _. Search and decisions refer to it.">
          <TextInput kind="code" value={entry.name} onChange={(name) => change({ name })} maxLength={63} />
        </Field>
        <Field label="Title">
          <TextInput value={entry.title} onChange={(title) => change({ title })} maxLength={200} />
        </Field>
      </FieldGrid>
      <Field label="Description">
        <TextArea value={entry.description} onChange={(description) => change({ description })} rows={2} />
      </Field>
      <MaskFields
        draft={draft}
        entry={entry}
        onMasks={(at, text) => onDraft(setMasks(draft, { ...place, code: at }, text))}
      />
      <AddCode
        services={unattachedServices(draft, place)}
        onAdd={(service) => onDraft(addCode(draft, place, service))}
      />
      <Actions>
        <ConfirmButton
          label={`Remove the ${what}`}
          question={`Remove this ${what} from the project? It is gone from the server when you save.`}
          confirmLabel="Remove"
          working="Removing…"
          onConfirm={async () => {
            onDraft(removeEntry(draft, place));
          }}
        />
      </Actions>
    </>
  );
}

interface MaskFieldsProps {
  /** The draft, for the names its services have now. */
  readonly draft: Project;
  readonly entry: Subdomain;
  /** Called with the position of the code and the text of its field. */
  readonly onMasks: (code: number, text: string) => void;
}

/** The path masks of an entry: one field per service it has code in. */
function MaskFields({ draft, entry, onMasks }: MaskFieldsProps): ReactElement {
  return (
    <>
      {entry.code.map((code, at) => (
        <Field
          key={`${code.project ?? ""}/${code.service}`}
          label={`Paths in ${shownService(draft, code)}`}
          hint="One mask per line, inside the service’s directory. Leave it empty to take this service out."
        >
          <TextArea value={code.paths.join("\n")} onChange={(text) => onMasks(at, text)} rows={3} />
        </Field>
      ))}
    </>
  );
}

interface AddCodeProps {
  /** The services of the project the entry has no code in yet. */
  readonly services: readonly NamedService[];
  readonly onAdd: (service: string) => void;
}

/** Attaches code of one more service of the project to the entry. */
function AddCode({ services, onAdd }: AddCodeProps): ReactElement | null {
  const [chosen, setChosen] = useState("");
  const service = services.some((item) => item.value === chosen) ? chosen : (services[0]?.value ?? "");
  if (service === "") {
    return null;
  }
  // The button stands outside the field: a field is a label, and a label holds one control.
  return (
    <>
      <Field label="Code in another service">
        <Select
          value={service}
          onChange={setChosen}
          options={services}
        />
      </Field>
      <Actions>
        <Button onClick={() => onAdd(service)}>Add paths in this service</Button>
      </Actions>
    </>
  );
}
