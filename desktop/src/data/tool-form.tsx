import type { ReactElement } from "react";

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
    return <p className="muted">This tool takes no arguments.</p>;
  }
  return (
    <div className="tool-fields">
      {tool.fields.map((field) => (
        <FieldRow key={`${field.location}:${field.name}`} field={field} values={values} onChange={onChange} />
      ))}
    </div>
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

function FieldRow({ field, values, onChange }: FieldRowProps): ReactElement {
  const note = noteOf(field);
  const control = (
    <FieldControl
      field={field}
      value={values.get(field.name)}
      onValue={(value) => {
        onChange(setValue(values, field.name, value));
      }}
      onToggle={(option) => {
        onChange(toggleChoice(values, field.name, option));
      }}
    />
  );
  const caption = (
    <>
      <span>
        <code>{field.name}</code> {note === "" ? null : <span className="muted small">{note}</span>}
      </span>
      {field.description === "" ? null : <small className="muted">{field.description}</small>}
    </>
  );
  // Several checkboxes are a group with a legend; any other control is labelled by its caption.
  if (field.kind === "choices") {
    return (
      <fieldset className="choices">
        <legend className="field-caption">{caption}</legend>
        {control}
      </fieldset>
    );
  }
  return (
    <label className="field">
      {caption}
      {control}
    </label>
  );
}

interface FieldControlProps {
  readonly field: ToolField;
  readonly value: FieldValue | undefined;
  readonly onValue: (value: string) => void;
  readonly onToggle: (option: string) => void;
}

function FieldControl({ field, value, onValue, onToggle }: FieldControlProps): ReactElement {
  const text = typeof value === "string" ? value : "";
  switch (field.kind) {
    case "choice":
      return (
        <select
          value={text}
          onChange={(event) => {
            onValue(event.target.value);
          }}
        >
          <option value="">{field.required ? "Choose…" : "Leave to the server"}</option>
          {field.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      );
    case "choices":
      return <ChoiceBoxes field={field} chosen={typeof value === "string" || value === undefined ? [] : value} onToggle={onToggle} />;
    case "list":
      return (
        <textarea
          rows={2}
          spellCheck={false}
          value={text}
          onChange={(event) => {
            onValue(event.target.value);
          }}
        />
      );
    case "integer":
    case "text":
      return (
        <input
          type="text"
          inputMode={field.kind === "integer" ? "numeric" : "text"}
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          value={text}
          onChange={(event) => {
            onValue(event.target.value);
          }}
        />
      );
  }
}

interface ChoiceBoxesProps {
  readonly field: ToolField;
  readonly chosen: readonly string[];
  readonly onToggle: (option: string) => void;
}

function ChoiceBoxes({ field, chosen, onToggle }: ChoiceBoxesProps): ReactElement {
  return (
    <div className="choice-row">
      {field.options.map((option) => (
        <label key={option} className="check">
          <input
            type="checkbox"
            checked={chosen.includes(option)}
            onChange={() => {
              onToggle(option);
            }}
          />
          <span>{option}</span>
        </label>
      ))}
    </div>
  );
}
