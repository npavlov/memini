import { describe, expect, it } from "vitest";

import {
  ShapeError,
  isObject,
  readArray,
  readBoolean,
  readNumber,
  readObject,
  readString,
  readStrings,
} from "./read-json";

describe("readString, readNumber and readBoolean", () => {
  it.each([
    ["a string", readString("a"), "a"],
    ["a number", readNumber(3), 3],
    ["a boolean", readBoolean(false), false],
  ])("return %s of their own type", (_what, read, expected) => {
    expect(read).toBe(expected);
  });

  it.each([
    ["a number read as a string", readString(1)],
    ["a string read as a number", readNumber("3")],
    ["not-a-number read as a number", readNumber(Number.NaN)],
    ["a number read as a boolean", readBoolean(0)],
    ["a missing value read as a string", readString(undefined)],
  ])("return null for %s", (_what, read) => {
    expect(read).toBeNull();
  });
});

describe("readArray and readStrings", () => {
  it.each([[undefined], [null], [{ length: 2 }], ["text"]])(
    "read %j, which is not a list, as an empty list",
    (value) => {
      expect(readArray(value)).toEqual([]);
    },
  );

  it("drop the items of a list that are not text", () => {
    expect(readStrings(["read", 7, null, "admin"])).toEqual(["read", "admin"]);
  });
});

describe("isObject and readObject", () => {
  it.each([
    [{}, true],
    [[], false],
    [null, false],
    ["text", false],
  ])("tell whether %j is an object", (value, expected) => {
    expect(isObject(value)).toBe(expected);
  });

  it("throw a ShapeError that names what was expected when the value is not an object", () => {
    const read = (): unknown => readObject(null, "the list of tokens");

    expect(read).toThrow(ShapeError);
    expect(read).toThrow("The answer is not the list of tokens.");
  });
});
