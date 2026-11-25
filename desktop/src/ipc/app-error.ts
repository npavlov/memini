// The error every command of the Rust side answers with: `{kind, message, status?, code?,
// request_id?}`. The message was masked and bounded in Rust; it is shown as text.

import { isObject, readNumber, readString } from "../json/read-json";
import { displayText } from "../text/display-text";

export const ERROR_KINDS = [
  "invalid",
  "not_found",
  "no_token",
  "keychain",
  "storage",
  "clipboard",
  "unreachable",
  "denied",
  "api",
  "unexpected",
  "launcher_missing",
  "launcher_failed",
  "launcher_output",
  "launcher_schema",
  "busy",
] as const;

export type ErrorKind = (typeof ERROR_KINDS)[number];

export interface AppErrorFields {
  readonly kind: ErrorKind;
  readonly message: string;
  readonly status?: number | null;
  readonly code?: string | null;
  readonly requestId?: string | null;
}

export class AppError extends Error {
  readonly kind: ErrorKind;
  readonly status: number | null;
  readonly code: string | null;
  readonly requestId: string | null;

  constructor(fields: AppErrorFields) {
    super(fields.message);
    this.name = "AppError";
    this.kind = fields.kind;
    this.status = fields.status ?? null;
    this.code = fields.code ?? null;
    this.requestId = fields.requestId ?? null;
  }
}

const GENERIC = "The app could not complete the request.";

function isErrorKind(value: unknown): value is ErrorKind {
  return ERROR_KINDS.some((kind) => kind === value);
}

/** Whatever a call rejected with, as an AppError. Text that is not the app's own is sanitized. */
export function toAppError(raw: unknown): AppError {
  if (raw instanceof AppError) {
    return raw;
  }
  if (isObject(raw) && isErrorKind(raw["kind"])) {
    return new AppError({
      kind: raw["kind"],
      message: displayText(readString(raw["message"]), 600) || GENERIC,
      status: readNumber(raw["status"]),
      code: readString(raw["code"]),
      requestId: readString(raw["request_id"]),
    });
  }
  // Tauri itself rejects with a string, for example for a command the capability does not allow.
  const text = typeof raw === "string" ? displayText(raw) : "";
  return new AppError({ kind: "unexpected", message: text || GENERIC });
}

/** What identifies the failure to an operator: the API's code, the HTTP status, the request id. */
export function errorReference(error: AppError): string | null {
  const parts = [
    error.code === null ? null : displayText(error.code, 80),
    error.status === null ? null : `HTTP ${String(error.status)}`,
    error.requestId === null ? null : `request ${displayText(error.requestId, 80)}`,
  ].filter((part): part is string => part !== null && part !== "");
  return parts.length === 0 ? null : parts.join(" · ");
}

/** What the user can do about the failure, when the app knows. */
export function errorAdvice(error: AppError): string | null {
  switch (error.kind) {
    case "no_token":
      return "Open the Connection screen and store a token for this connection.";
    case "denied":
      return error.status === 403
        ? "The token is valid but lacks the scope this needs: read for Status, admin for Tokens."
        : "The token may be revoked or expired. Store a current one on the Connection screen.";
    case "launcher_output":
    case "launcher_schema":
      return "Install the versions of this app and of memini-server that belong together.";
    case "keychain":
      return "The token stays in the system keychain; the app cannot work around it.";
    default:
      return null;
  }
}
