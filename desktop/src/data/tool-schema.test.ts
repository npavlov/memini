import { describe, expect, it } from "vitest";

import { ShapeError } from "../json/read-json";
import { parseTools } from "./tool-schema";
import type { ToolField } from "./tool-schema";

/** A parameter as the server's OpenAPI document writes one, with the shapes the service uses. */
function parameter(name: string, schema: unknown, extra: Record<string, unknown> = {}): unknown {
  return { name, in: "query", required: false, schema, ...extra };
}

function fieldOf(schema: unknown, extra: Record<string, unknown> = {}): ToolField | undefined {
  const tools = parseTools([
    { name: "search", path: "/v1/search", parameters: [parameter("it", schema, extra)] },
  ]);
  return tools[0]?.fields[0];
}

const nullable = (schema: unknown): unknown => ({ anyOf: [schema, { type: "null" }] });

describe("parseTools", () => {
  it("reads a tool with its route, its words and its fields in the server's order", () => {
    const tools = parseTools([
      {
        name: "get_decision",
        path: "/v1/decisions/{item_id}",
        summary: "Get a decision",
        description: "With its chain.",
        spanning: [],
        parameters: [
          { name: "item_id", in: "path", required: true, schema: { type: "string", format: "uuid" } },
          parameter("history", { type: "boolean", default: false }),
        ],
      },
    ]);

    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({
      name: "get_decision",
      path: "/v1/decisions/{item_id}",
      summary: "Get a decision",
      description: "With its chain.",
    });
    expect(tools[0]?.fields.map((field) => field.name)).toEqual(["item_id", "history"]);
  });

  it.each([
    ["plain text", { type: "string", maxLength: 4096 }, "text"],
    ["a nullable text", nullable({ type: "string" }), "text"],
    ["a number", { type: "integer", minimum: 1, maximum: 100 }, "integer"],
    ["a nullable number", nullable({ type: "integer", minimum: 1 }), "integer"],
    ["one of some words", { type: "string", enum: ["hybrid", "dense"] }, "choice"],
    ["yes or no", { type: "boolean", default: true }, "choice"],
    ["a list of text", nullable({ type: "array", items: { type: "string" } }), "list"],
    ["a list of words", { type: "array", items: { type: "string", enum: ["a", "b"] } }, "choices"],
    ["a shape the app does not know", { type: "object" }, "text"],
    ["no schema at all", null, "text"],
  ] as const)("makes a field for %s", (_what, schema, kind) => {
    expect(fieldOf(schema)?.kind).toBe(kind);
  });

  it("offers the words of an enumeration, and true and false for a yes or no", () => {
    expect(fieldOf({ type: "string", enum: ["semantic", "exact"] })?.options).toEqual(["semantic", "exact"]);
    expect(fieldOf({ type: "boolean" })?.options).toEqual(["true", "false"]);
  });

  it("puts the enumerations of a list's alternatives together", () => {
    const kinds = nullable({
      type: "array",
      items: {
        anyOf: [
          { type: "string", enum: ["thread", "meeting"] },
          { type: "string", enum: ["decision"] },
        ],
      },
    });

    expect(fieldOf(kinds)).toMatchObject({ kind: "choices", options: ["thread", "meeting", "decision"] });
  });

  it.each([
    ["a text default", { type: "string", default: "me" }, "me"],
    ["a number default", { type: "integer", default: 10 }, "10"],
    ["a yes or no default", { type: "boolean", default: false }, "false"],
    ["no default", { type: "string" }, null],
  ])("shows %s as text", (_what, schema, shown) => {
    expect(fieldOf(schema)?.defaultText).toBe(shown);
  });

  it.each([
    ["a range", { type: "integer", minimum: 1, maximum: 100 }, "1 to 100"],
    ["a lower bound", { type: "integer", minimum: 1 }, "1 or more"],
    ["a time", nullable({ type: "string", format: "date-time" }), "A time with a zone, such as 2026-03-01T00:00:00Z"],
    ["an id", { type: "string", format: "uuid" }, "An id, as results give it"],
    ["a list", { type: "array", items: { type: "string" } }, "One value per line"],
  ])("hints at what a value looks like for %s", (_what, schema, hint) => {
    expect(fieldOf(schema)?.hint).toBe(hint);
  });

  it("takes the description from the parameter and puts it on one line", () => {
    const field = fieldOf({ type: "string" }, { description: "A question\nor keywords." });

    expect(field?.description).toBe("A question or keywords.");
  });

  it("lets a placeholder that holds a path of its own stay empty", () => {
    const tools = parseTools([
      {
        name: "read_source",
        path: "/v1/workspaces/{workspace}/sources/{source}/files/{path}",
        spanning: ["path"],
        parameters: [
          { name: "source", in: "path", required: true, schema: { type: "string" } },
          { name: "path", in: "path", required: true, schema: { type: "string" } },
        ],
      },
    ]);

    expect(tools[0]?.fields.map((field) => field.required)).toEqual([true, false]);
  });

  it.each([
    ["has no name", { in: "query", schema: { type: "string" } }],
    ["is sent in a header", { name: "x-key", in: "header", schema: { type: "string" } }],
    ["is not an object", "query"],
  ])("leaves out a parameter that %s", (_what, odd) => {
    const tools = parseTools([{ name: "search", path: "/v1/search", parameters: [odd] }]);

    expect(tools[0]?.fields).toEqual([]);
  });

  it("reads an answer that is not a list as no tools", () => {
    expect(parseTools({ tools: [] })).toEqual([]);
  });

  it("throws a ShapeError when a tool is not an object", () => {
    expect(() => parseTools(["search"])).toThrow(ShapeError);
  });
});
