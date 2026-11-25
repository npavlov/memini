// Readers for values that crossed a boundary: an answer of the Rust side, which may carry what a
// server sent. Nothing is cast: a value is used only after it was looked at.

/** A value that came over a boundary does not have the shape the app expects. */
export class ShapeError extends Error {
  constructor(what: string) {
    super(`The answer is not ${what}.`);
    this.name = "ShapeError";
  }
}

export type JsonObject = Readonly<Record<string, unknown>>;

export function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The value as an object; throws a ShapeError that names `what` was expected. */
export function readObject(value: unknown, what: string): JsonObject {
  if (!isObject(value)) {
    throw new ShapeError(what);
  }
  return value;
}

export function readString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function readBoolean(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function readArray(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

export function readStrings(value: unknown): readonly string[] {
  return readArray(value).filter((item): item is string => typeof item === "string");
}
