import { describe, expect, it } from "vitest";

import { AppError, errorAdvice, errorReference, toAppError } from "./app-error";

const GENERIC = "The app could not complete the request.";

describe("toAppError", () => {
  it("reads every field of the error shape of the Rust side", () => {
    const error = toAppError({
      kind: "denied",
      message: "The token lacks the admin scope.",
      status: 403,
      code: "insufficient_scope",
      request_id: "req-1",
    });

    expect(error).toBeInstanceOf(AppError);
    expect(error).toMatchObject({
      kind: "denied",
      message: "The token lacks the admin scope.",
      status: 403,
      code: "insufficient_scope",
      requestId: "req-1",
    });
  });

  it("returns an AppError unchanged", () => {
    const error = new AppError({ kind: "busy", message: "Wait." });

    expect(toAppError(error)).toBe(error);
  });

  it("shows a rejection of Tauri itself, which is a string, as sanitized text", () => {
    const error = toAppError("Command api_status not allowed by ACL\n");

    expect(error.kind).toBe("unexpected");
    expect(error.message).toBe("Command api_status not allowed by ACL");
  });

  it.each([
    ["an unknown kind", { kind: "made_up", message: "x" }],
    ["nothing", undefined],
    ["a message that is not text", { kind: "api", message: 5 }],
    ["another error, whose text is internal", new Error("internal detail")],
  ])("answers with the app's own words for %s", (_what, raw) => {
    expect(toAppError(raw).message).toBe(GENERIC);
  });

  it("puts a server's message on one line", () => {
    const error = toAppError({ kind: "api", message: "first\nsecond" });

    expect(error.message).toBe("first second");
  });
});

describe("errorReference", () => {
  it("joins the code, the status and the request id of an API error", () => {
    const error = new AppError({
      kind: "api",
      message: "No.",
      status: 422,
      code: "validation_error",
      requestId: "abc",
    });

    expect(errorReference(error)).toBe("validation_error · HTTP 422 · request abc");
  });

  it("returns null for an error of the app itself", () => {
    expect(errorReference(new AppError({ kind: "invalid", message: "No." }))).toBeNull();
  });
});

describe("errorAdvice", () => {
  it.each([
    ["a refused token (401)", { kind: "denied", status: 401 } as const, "revoked or expired"],
    ["a missing scope (403)", { kind: "denied", status: 403 } as const, "lacks the scope"],
    ["a connection without a token", { kind: "no_token" } as const, "Connection screen"],
    ["an unknown launcher schema", { kind: "launcher_schema" } as const, "belong together"],
  ])("says what to do about %s", (_what, fields, advice) => {
    const error = new AppError({ message: "No.", ...fields });

    expect(errorAdvice(error)).toContain(advice);
  });

  it("returns null when the message is all there is to say", () => {
    expect(errorAdvice(new AppError({ kind: "unreachable", message: "Away." }))).toBeNull();
  });
});
