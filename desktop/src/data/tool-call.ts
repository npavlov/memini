// From what the user entered in a tool's form to the call the Rust side runs. The front end
// names the tool and gives values; Rust owns the route and sends a GET, nothing else.

import type { CommandArguments } from "../ipc/backend";
import type { Tool, ToolField } from "./tool-schema";

/** What a field holds: text, or for several choices the chosen ones. */
export type FieldValue = string | readonly string[];

/** The form's values by field name. A map, because the names are the server's. */
export type FormValues = ReadonlyMap<string, FieldValue>;

function textOf(value: FieldValue | undefined): string {
  return typeof value === "string" ? value.trim() : "";
}

/** The values a field stands for: none when it is left empty, several for a list. */
function valuesOf(field: ToolField, value: FieldValue | undefined): readonly string[] {
  if (field.kind === "choices") {
    return typeof value === "string" || value === undefined ? [] : value;
  }
  if (field.kind === "list") {
    return textOf(value)
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line !== "");
  }
  const text = textOf(value);
  return text === "" ? [] : [text];
}

export function setValue(values: FormValues, name: string, value: FieldValue): FormValues {
  return new Map([...values, [name, value]]);
}

export function toggleChoice(values: FormValues, name: string, option: string): FormValues {
  const current = values.get(name);
  const chosen = typeof current === "string" || current === undefined ? [] : current;
  const next = chosen.includes(option)
    ? chosen.filter((item) => item !== option)
    : [...chosen, option];
  return setValue(values, name, next);
}

function checkField(field: ToolField, value: FieldValue | undefined): string | null {
  const given = valuesOf(field, value);
  if (field.required && given.length === 0) {
    return `${field.name} is needed.`;
  }
  if (field.kind === "integer" && given.some((item) => !/^-?\d+$/u.test(item))) {
    return `${field.name} is a whole number.`;
  }
  return null;
}

/** The first problem with the form, or null. A hint: the server checks every argument itself. */
export function checkValues(tool: Tool, values: FormValues): string | null {
  return (
    tool.fields
      .map((field) => checkField(field, values.get(field.name)))
      .find((problem) => problem !== null) ?? null
  );
}

/** The form as the `api_tool_call` command takes it. An empty field is left to the server's default. */
export function toToolCall(tool: Tool, values: FormValues): CommandArguments {
  const inPath = tool.fields.filter((field) => field.location === "path");
  const inQuery = tool.fields.filter((field) => field.location === "query");
  return {
    tool: tool.name,
    path: Object.fromEntries(inPath.map((field) => [field.name, textOf(values.get(field.name))])),
    query: inQuery.flatMap((field) =>
      valuesOf(field, values.get(field.name)).map((value) => [field.name, value]),
    ),
  };
}
