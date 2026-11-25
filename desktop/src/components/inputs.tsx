import type { ReactElement, ReactNode } from "react";

/** Prose is ordinary text; code is an address, a path or an id, in the mono font; a number is digits. */
export type InputKind = "prose" | "code" | "number";

export interface TextInputProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly kind?: InputKind;
  readonly placeholder?: string;
  readonly maxLength?: number;
}

/** A line of text. The browser does not correct or capitalize code and numbers. */
export function TextInput(props: TextInputProps): ReactElement {
  const { value, onChange, kind = "prose" } = props;
  const literal = kind !== "prose";
  return (
    <input
      type="text"
      className={kind === "code" ? "input input-code" : "input"}
      inputMode={kind === "number" ? "numeric" : "text"}
      spellCheck={literal ? false : undefined}
      autoCapitalize={literal ? "off" : undefined}
      autoCorrect={literal ? "off" : undefined}
      value={value}
      placeholder={props.placeholder}
      maxLength={props.maxLength}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}

export interface TextAreaProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly rows?: number;
}

/** Several lines of literal text, such as a list with one value per line. */
export function TextArea({ value, onChange, rows = 2 }: TextAreaProps): ReactElement {
  return (
    <textarea
      className="input input-code input-lines"
      rows={rows}
      spellCheck={false}
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}

export interface SelectOption {
  readonly value: string;
  readonly label: string;
}

export interface SelectProps {
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly options: readonly SelectOption[];
}

/** One of several options. An option's label is text; its value is what `onChange` gets. */
export function Select({ value, onChange, options }: SelectProps): ReactElement {
  return (
    <select
      className="input"
      value={value}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  );
}

export interface CheckboxProps {
  readonly checked: boolean;
  readonly onChange: (checked: boolean) => void;
  /** What ticking the box means. */
  readonly children: ReactNode;
}

/** A box to tick, with its words. */
export function Checkbox({ checked, onChange, children }: CheckboxProps): ReactElement {
  return (
    <label className="check">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => {
          onChange(event.target.checked);
        }}
      />
      <span>{children}</span>
    </label>
  );
}
