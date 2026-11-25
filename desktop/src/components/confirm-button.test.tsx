import { describe, expect, it } from "vitest";

import { ConfirmButton } from "./confirm-button";
import { markupOf } from "./markup";

const confirm = async (): Promise<void> => undefined;

describe("ConfirmButton", () => {
  it("shows one ordinary button before it is clicked, and no question", () => {
    const markup = markupOf(
      <ConfirmButton label="Delete" question="Delete this?" confirmLabel="Yes, delete" onConfirm={confirm} />,
    );

    expect(markup).toBe('<button type="button" class="button button-secondary">Delete</button>');
  });

  it("is disabled when told so", () => {
    const markup = markupOf(
      <ConfirmButton label="Delete" question="Delete this?" confirmLabel="Delete" disabled onConfirm={confirm} />,
    );

    expect(markup).toContain("disabled");
  });
});
