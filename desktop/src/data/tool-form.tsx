import type { ReactElement } from "react";

import { Checkbox, Field, FieldGrid, FieldGroup, Mono, Select, Text, TextArea, TextInput } from "../components";
import { setValue, toggleChoice } from "./tool-call";
import type { FieldValue, FormValues } from "./tool-call";
import type { Tool, ToolField } from "./tool-schema";

export interface ToolFieldsProps {
  readonly tool: Tool;
  readonly values: FormValues;
  readonly onChange: (values: FormValues) => void;
}

/** The fields of a tool's form, in the order the server lists its arguments. */
export function ToolFields({ tool, values, onChange }: ToolFieldsProps): ReactElement {
  if (tool.fields.length === 0) {
    return <Text tone="muted">This tool takes no arguments.</Text>;
  }
  return (
    <FieldGrid>
      {tool.fields.map((field) => (
        <FieldRow key={`${field.location}:${field.name}`} field={field} values={values} onChange={onChange} />
      ))}
    </FieldGrid>
  );
}

interface FieldRowProps {
  readonly field: ToolField;
  readonly values: FormValues;
  readonly onChange: (values: FormValues) => void;
}

function noteOf(field: ToolField): string {
  const parts = [
    field.required ? "needed" : null,
    field.location === "path" ? "part of the address" : null,
    field.hint === "" ? null : field.hint,
    field.defaultText === null ? null : `default: ${field.defaultText}`,
  ];
  return parts.filter((part): part is string => part !== null).join(" · ");
}

function chosenOf(value: FieldValue | undefined): readonly string[] {
  return typeof value === "string" || value === undefined ? [] : value;
}

function FieldRow({ field, values, onChange }: FieldRowProps): ReactElement {
  const label = <Mono>{field.name}</Mono>;
  const value = values.get(field.name);
  // Several boxes are a group with a legend; any other control is labelled by its field.
  if (field.kind === "choices") {
    const chosen = chosenOf(value);
    return (
      <FieldGroup label={label} note={noteOf(field)} hint={field.description} inline>
        {field.options.map((option) => (
          <Checkbox
            key={option}
            checked={chosen.includes(option)}
            onChange={() => {
              onChange(toggleChoice(values, field.name, option));
            }}
          >
            {option}
          </Checkbox>
        ))}
      </FieldGroup>
    );
  }
  return (
    <Field label={label} note={noteOf(field)} hint={field.description}>
      <FieldControl
        field={field}
        text={typeof value === "string" ? value : ""}
        onText={(text) => {
          onChange(setValue(values, field.name, text));
        }}
      />
    </Field>
  );
}

interface FieldControlProps {
  readonly field: ToolField;
  readonly text: string;
  readonly onText: (text: string) => void;
}

function FieldControl({ field, text, onText }: FieldControlProps): ReactElement {
  switch (field.kind) {
    case "choice": {
      const unset = { value: "", label: field.required ? "Choose…" : "Leave to the server" };
      const options = field.options.map((option) => ({ value: option, label: option }));
      return <Select value={text} options={[unset, ...options]} onChange={onText} />;
    }
    case "list":
      return <TextArea value={text} onChange={onText} />;
    case "integer":
      return <TextInput kind="number" value={text} onChange={onText} />;
    case "choices":
    case "text":
      return <TextInput kind="code" value={text} onChange={onText} />;
  }
}
