import { describe, expect, it } from "vitest";

import { checkValues, setValue, toToolCall, toggleChoice } from "./tool-call";
import type { FieldValue, FormValues } from "./tool-call";
import type { FieldKind, Tool, ToolField } from "./tool-schema";

function field(name: string, kind: FieldKind, changes: Partial<ToolField> = {}): ToolField {
  return {
    name,
    location: "query",
    required: false,
    kind,
    options: [],
    defaultText: null,
    description: "",
    hint: "",
    ...changes,
  };
}

const search: Tool = {
  name: "search",
  path: "/v1/search",
  summary: "Search everything",
  description: "",
  fields: [
    field("query", "text", { required: true }),
    field("limit", "integer", { defaultText: "10" }),
    field("mode", "choice", { options: ["hybrid", "dense"] }),
    field("kinds", "choices", { options: ["thread", "meeting"] }),
    field("sources", "list"),
  ],
};

const readSource: Tool = {
  name: "get_file",
  path: "/v1/workspaces/{workspace}/sources/{source}/files/{path}",
  summary: "",
  description: "",
  fields: [
    field("workspace", "text", { location: "path", required: true }),
    field("source", "text", { location: "path", required: true }),
    field("path", "text", { location: "path" }),
    field("start_line", "integer"),
  ],
};

function values(entries: Record<string, FieldValue>): FormValues {
  return new Map(Object.entries(entries));
}

describe("checkValues", () => {
  it("accepts a form whose needed fields are filled", () => {
    expect(checkValues(search, values({ query: "archive retention" }))).toBeNull();
  });

  it.each([
    ["a needed field is empty", values({}), "query is needed."],
    ["a needed field is only spaces", values({ query: "   " }), "query is needed."],
    ["a number is not a whole number", values({ query: "x", limit: "ten" }), "limit is a whole number."],
  ])("reports the problem when %s", (_what, given, problem) => {
    expect(checkValues(search, given)).toBe(problem);
  });

  it("accepts an empty path for the root of a repository", () => {
    const given = values({ workspace: "harbour", source: "harbour-notes" });

    expect(checkValues(readSource, given)).toBeNull();
  });
});

describe("toToolCall", () => {
  it("leaves an empty field to the server's default", () => {
    const call = toToolCall(search, values({ query: " archive retention ", limit: "" }));

    expect(call).toEqual({ tool: "search", path: {}, query: [["query", "archive retention"]] });
  });

  it("repeats the name for each chosen option and each line of a list", () => {
    const call = toToolCall(
      search,
      values({ query: "x", kinds: ["thread", "meeting"], sources: "chat\n\n mail \n", mode: "dense" }),
    );

    expect(call["query"]).toEqual([
      ["query", "x"],
      ["mode", "dense"],
      ["kinds", "thread"],
      ["kinds", "meeting"],
      ["sources", "chat"],
      ["sources", "mail"],
    ]);
  });

  it("puts the route's placeholders into the path, an empty one included", () => {
    const call = toToolCall(
      readSource,
      values({ workspace: "harbour", source: "harbour-notes", start_line: "5" }),
    );

    expect(call).toEqual({
      tool: "get_file",
      path: { workspace: "harbour", source: "harbour-notes", path: "" },
      query: [["start_line", "5"]],
    });
  });
});

describe("setValue and toggleChoice", () => {
  it("set a field without changing the values they were given", () => {
    const before = values({ query: "a" });

    const after = setValue(before, "query", "b");

    expect(after.get("query")).toBe("b");
    expect(before.get("query")).toBe("a");
  });

  it("add an option that is not chosen", () => {
    expect(toggleChoice(values({}), "kinds", "thread").get("kinds")).toEqual(["thread"]);
  });

  it("remove an option that is chosen", () => {
    const chosen = values({ kinds: ["thread", "meeting"] });

    expect(toggleChoice(chosen, "kinds", "thread").get("kinds")).toEqual(["meeting"]);
  });
});
