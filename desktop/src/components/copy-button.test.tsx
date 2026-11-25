import { describe, expect, it } from "vitest";

import { CopyButton } from "./copy-button";
import { markupOf } from "./markup";

describe("CopyButton", () => {
  it("is a button that says Copy by default", () => {
    expect(markupOf(<CopyButton text="memini status" />)).toBe(
      '<button type="button" class="button button-secondary">Copy</button>',
    );
  });

  it("takes another label", () => {
    expect(markupOf(<CopyButton text="x" label="Copy address" />)).toContain(">Copy address<");
  });

  it("never renders the text it copies, which may be a secret", () => {
    expect(markupOf(<CopyButton text="the-secret-value" secret />)).not.toContain("the-secret-value");
  });
});
