import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { Pill } from "./pill";

describe("Pill", () => {
  it.each(["good", "warn", "bad", "info", "idle"] as const)("is a chip in the %s tone", (tone) => {
    expect(markupOf(<Pill tone={tone}>current</Pill>)).toBe(`<span class="pill pill-${tone}">current</span>`);
  });

  it("shows markup in its words as text", () => {
    expect(markupOf(<Pill tone="info">{"<b>x</b>"}</Pill>)).toContain("&lt;b&gt;x&lt;/b&gt;");
  });
});
