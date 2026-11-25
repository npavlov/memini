// The read tools as the server publishes them. Rust took each tool's operation from the server's
// OpenAPI document; this module turns its parameters into form fields. Nothing about a tool's
// arguments is written down in the app, so a filter the server adds shows up by itself.

import { isObject, readArray, readBoolean, readNumber, readObject, readString } from "../json/read-json";
import type { JsonObject } from "../json/read-json";
import { displayText } from "../text/display-text";

/** How a field is entered: free text, a number, one of some options, several of them, or a list. */
export type FieldKind = "text" | "integer" | "choice" | "choices" | "list";

export interface ToolField {
  readonly name: string;
  /** A placeholder of the route, or an argument in the query. */
  readonly location: "path" | "query";
  readonly required: boolean;
  readonly kind: FieldKind;
  /** The values a choice may take; empty for the other kinds. */
  readonly options: readonly string[];
  /** The server's default, as text; null when it has none. */
  readonly defaultText: string | null;
  readonly description: string;
  /** What a value looks like: a range, a format. */
  readonly hint: string;
}

export interface Tool {
  readonly name: string;
  /** The route's template, for the reader. */
  readonly path: string;
  readonly summary: string;
  readonly description: string;
  readonly fields: readonly ToolField[];
}

interface Shape {
  readonly type: string | null;
  readonly format: string | null;
  readonly options: readonly string[];
  readonly items: Shape | null;
  readonly minimum: number | null;
  readonly maximum: number | null;
}

const NO_SHAPE: Shape = { type: null, format: null, options: [], items: null, minimum: null, maximum: null };
const MAX_DEPTH = 6;
const BOOLEAN_OPTIONS = ["true", "false"];

function scalarText(value: unknown): string | null {
  if (typeof value === "string") {
    return value;
  }
  return typeof value === "number" || typeof value === "boolean" ? String(value) : null;
}

function optionsOf(schema: JsonObject): readonly string[] {
  const listed = readArray(schema["enum"]).map(scalarText);
  const constant = scalarText(schema["const"]);
  return [...listed, constant]
    .filter((option): option is string => option !== null)
    .map((option) => displayText(option, 80));
}

/** One shape for the alternatives of an anyOf: null is dropped, and enums are put together. */
function mergeVariants(variants: readonly Shape[]): Shape {
  const real = variants.filter((variant) => variant.type !== "null");
  const first = real[0];
  if (first === undefined) {
    return NO_SHAPE;
  }
  const allChoices = real.every((variant) => variant.options.length > 0);
  return allChoices ? { ...first, options: real.flatMap((variant) => variant.options) } : first;
}

function shapeOf(schema: unknown, depth: number): Shape {
  if (!isObject(schema) || depth > MAX_DEPTH) {
    return NO_SHAPE;
  }
  const variants = [...readArray(schema["anyOf"]), ...readArray(schema["oneOf"])];
  if (variants.length > 0) {
    return mergeVariants(variants.map((variant) => shapeOf(variant, depth + 1)));
  }
  const type = readString(schema["type"]);
  return {
    type,
    format: readString(schema["format"]),
    options: type === "boolean" ? BOOLEAN_OPTIONS : optionsOf(schema),
    items: type === "array" ? shapeOf(schema["items"], depth + 1) : null,
    minimum: readNumber(schema["minimum"]),
    maximum: readNumber(schema["maximum"]),
  };
}

function kindOf(shape: Shape): FieldKind {
  if (shape.type === "array") {
    return (shape.items?.options.length ?? 0) > 0 ? "choices" : "list";
  }
  if (shape.options.length > 0) {
    return "choice";
  }
  return shape.type === "integer" || shape.type === "number" ? "integer" : "text";
}

function rangeHint(shape: Shape): string {
  if (shape.minimum !== null && shape.maximum !== null) {
    return `${String(shape.minimum)} to ${String(shape.maximum)}`;
  }
  if (shape.minimum !== null) {
    return `${String(shape.minimum)} or more`;
  }
  return shape.maximum === null ? "A number" : `${String(shape.maximum)} or less`;
}

function hintOf(shape: Shape, kind: FieldKind): string {
  if (kind === "list") {
    return "One value per line";
  }
  if (kind === "integer") {
    return rangeHint(shape);
  }
  if (shape.format === "date-time") {
    return "A time with a zone, such as 2026-03-01T00:00:00Z";
  }
  return shape.format === "uuid" ? "An id, as results give it" : "";
}

function parseField(value: unknown, spanning: readonly string[]): ToolField | null {
  if (!isObject(value)) {
    return null;
  }
  const name = readString(value["name"]);
  const location = value["in"];
  if (name === null || name === "" || (location !== "path" && location !== "query")) {
    return null;
  }
  const schema = isObject(value["schema"]) ? value["schema"] : {};
  const shape = shapeOf(schema, 0);
  const kind = kindOf(shape);
  const described = readString(value["description"]) ?? readString(schema["description"]);
  return {
    name,
    location,
    // A placeholder that holds a path of its own may stay empty: the root of a repository.
    required: (readBoolean(value["required"]) ?? false) && !spanning.includes(name),
    kind,
    options: kind === "choices" ? (shape.items?.options ?? []) : shape.options,
    defaultText: scalarText(schema["default"]),
    description: displayText(described, 600),
    hint: hintOf(shape, kind),
  };
}

function parseTool(value: unknown): Tool {
  const tool = readObject(value, "a tool of the server");
  const spanning = readArray(tool["spanning"]).filter((item): item is string => typeof item === "string");
  return {
    name: displayText(readString(tool["name"]), 60),
    path: displayText(readString(tool["path"]), 200),
    summary: displayText(readString(tool["summary"]), 200),
    description: displayText(readString(tool["description"]), 600),
    fields: readArray(tool["parameters"])
      .map((parameter) => parseField(parameter, spanning))
      .filter((field): field is ToolField => field !== null),
  };
}

/** The tools of the `api_tools` command. */
export function parseTools(value: unknown): readonly Tool[] {
  return readArray(value).map(parseTool);
}
