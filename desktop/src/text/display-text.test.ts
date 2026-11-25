import { describe, expect, it } from "vitest";

import { displayText } from "./display-text";

const at = (codePoint: number): string => String.fromCodePoint(codePoint);

describe("displayText", () => {
  it("returns ordinary text unchanged, other scripts included", () => {
    expect(displayText("harbour-notes / Übersicht / 港")).toBe("harbour-notes / Übersicht / 港");
  });

  it("returns markup as the text it is", () => {
    expect(displayText('<img src=x onerror="run()">')).toBe('<img src=x onerror="run()">');
  });

  it.each([
    ["a line break", "first\r\nsecond", "first second"],
    ["a tab", "first\tsecond", "first second"],
    ["an escape character", `red${at(0x1b)}[31m`, "red [31m"],
    ["a right-to-left override", `safe${at(0x202e)}exe.txt`, "safe exe.txt"],
    ["a zero-width space", `pay${at(0x200b)}load`, "pay load"],
    ["a directional isolate", `a${at(0x2066)}b`, "a b"],
    ["a line separator", `a${at(0x2028)}b`, "a b"],
    ["a byte order mark", `${at(0xfeff)}name`, "name"],
  ])("replaces %s with a space", (_what, text, shown) => {
    expect(displayText(text)).toBe(shown);
  });

  it.each([
    ["longer than the limit", "abcdef", 3, "abc…"],
    ["exactly at the limit", "abc", 3, "abc"],
    ["made of characters outside the basic plane", "🙂🙂🙂🙂", 2, "🙂🙂…"],
  ])("bounds text %s by characters", (_what, text, limit, shown) => {
    expect(displayText(text, limit)).toBe(shown);
  });

  it("returns an empty text for a missing value", () => {
    expect(displayText(null)).toBe("");
  });
});
