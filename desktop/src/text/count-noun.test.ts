import { describe, expect, it } from "vitest";

import { countNoun } from "./count-noun";

describe("countNoun", () => {
  it.each([
    [0, "0 streams"],
    [1, "1 stream"],
    [2, "2 streams"],
  ])("writes %d with the noun in the right number", (count, words) => {
    expect(countNoun(count, "stream")).toBe(words);
  });

  it("uses the plural it is given for a noun that does not take an s", () => {
    expect(countNoun(3, "index", "indexes")).toBe("3 indexes");
  });
});
