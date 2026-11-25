import type { ReactElement, ReactNode } from "react";

export interface FieldProps {
  /** The name of the field. */
  readonly label: ReactNode;
  /** A few words beside the label: whether it is needed, its default. */
  readonly note?: string;
  /** A sentence under the control on what to enter. */
  readonly hint?: string;
  /** Lets the field take the free width of a row it shares with a button. */
  readonly grow?: boolean;
  /** The control: a TextInput, a TextArea or a Select. */
  readonly children: ReactNode;
}

interface CaptionProps {
  readonly label: ReactNode;
  readonly note: string | undefined;
}

function Caption({ label, note }: CaptionProps): ReactElement {
  return (
    <span className="field-label">
      {label}
      {note === undefined || note === "" ? null : <span className="field-note">{note}</span>}
    </span>
  );
}

/** A control with its label and hint. Clicking the label reaches the control. */
export function Field({ label, note, hint, grow = false, children }: FieldProps): ReactElement {
  return (
    <label className={grow ? "field field-grow" : "field"}>
      <Caption label={label} note={note} />
      {children}
      {hint === undefined || hint === "" ? null : <small className="field-hint">{hint}</small>}
    </label>
  );
}

export interface FieldGroupProps {
  readonly label: ReactNode;
  readonly note?: string;
  readonly hint?: string;
  /** Lays the controls out in a row that wraps, for short options. */
  readonly inline?: boolean;
  /** The controls of the group, usually Checkboxes. */
  readonly children: ReactNode;
}

/** Several controls that answer one question, such as the boxes of a choice. */
export function FieldGroup(props: FieldGroupProps): ReactElement {
  const { label, note, hint, inline = false, children } = props;
  return (
    <fieldset className="field field-group">
      <legend>
        <Caption label={label} note={note} />
      </legend>
      <div className={inline ? "field-options field-options-inline" : "field-options"}>{children}</div>
      {hint === undefined || hint === "" ? null : <small className="field-hint">{hint}</small>}
    </fieldset>
  );
}
