import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import { markupOf } from "./markup";
import { ErrorNotice, Notice } from "./notice";

describe("Notice", () => {
  it.each(["good", "warn", "info", "idle"] as const)("is a status in the %s tone", (tone) => {
    const markup = markupOf(<Notice tone={tone} title="Done" />);

    expect(markup).toBe(`<div class="notice notice-${tone}" role="status"><p class="notice-title">Done</p></div>`);
  });

  it("is announced as an alert when it is bad", () => {
    expect(markupOf(<Notice tone="bad" title="Failed" />)).toContain('role="alert"');
  });

  it("shows its content under the title", () => {
    const markup = markupOf(
      <Notice tone="info" title="Note">
        <p>more</p>
      </Notice>,
    );

    expect(markup).toContain('<p class="notice-title">Note</p><p>more</p>');
  });
});

describe("ErrorNotice", () => {
  it("renders nothing without an error", () => {
    expect(markupOf(<ErrorNotice error={null} />)).toBe("");
  });

  it("shows the message, what to do about it, and how to find it in a log", () => {
    const error = new AppError({
      kind: "denied",
      message: "The token lacks the admin scope.",
      status: 403,
      code: "insufficient_scope",
      requestId: "req-1",
    });

    const markup = markupOf(<ErrorNotice error={error} />);

    expect(markup).toContain('<p class="notice-title">The token lacks the admin scope.</p>');
    expect(markup).toContain("lacks the scope this needs");
    expect(markup).toContain("insufficient_scope · HTTP 403 · request req-1");
  });

  it("shows markup in a server's message as text", () => {
    const error = new AppError({ kind: "api", message: "<img src=x onerror=run()>" });

    expect(markupOf(<ErrorNotice error={error} />)).not.toContain("<img");
  });
});
