import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import { createFakeBackend } from "../ipc/fake-backend";
import { callTool, listTools, readOverview } from "./data-api";

/** The status route's answer for one workspace with one git stream. */
const statusAnswer = {
  version: "0.2.0",
  current: true,
  database: { reachable: true },
  index: { state: "ok" },
  workspaces: [
    {
      name: "harbour",
      index: { state: "ok", records: 100, chunks: 900 },
      streams: [{ source: "harbour-notes", source_kind: "git", key: "refs/heads/main", records: 100 }],
    },
  ],
};

describe("readOverview", () => {
  it("joins the status with each workspace's list of sources", async () => {
    const backend = createFakeBackend({
      answers: {
        api_status: () => statusAnswer,
        api_sources: () => ({
          sources: [
            { name: "chat", kind: "push", schedule_seconds: null },
            { name: "harbour-notes", kind: "git", schedule_seconds: 3600 },
          ],
        }),
      },
    });

    const overview = await readOverview(backend, "c1");

    expect(overview[0]?.sources.map((row) => [row.name, row.records])).toEqual([
      ["chat", 0],
      ["harbour-notes", 100],
    ]);
    expect(backend.calls[1]).toEqual({ command: "api_sources", args: { id: "c1", workspace: "harbour" } });
  });

  it("still shows the sources of the status when the list of sources is refused", async () => {
    const backend = createFakeBackend({
      answers: {
        api_status: () => statusAnswer,
        api_sources: () => {
          throw new AppError({ kind: "denied", message: "No.", status: 403 });
        },
      },
    });

    const overview = await readOverview(backend, "c1");

    expect(overview[0]?.sources.map((row) => row.name)).toEqual(["harbour-notes"]);
  });
});

describe("the tool commands", () => {
  it("list the tools of a connection by its id", async () => {
    const backend = createFakeBackend({
      answers: { api_tools: () => [{ name: "search", path: "/v1/search", parameters: [] }] },
    });

    const tools = await listTools(backend, "c1");

    expect(tools.map((tool) => tool.name)).toEqual(["search"]);
    expect(backend.calls).toEqual([{ command: "api_tools", args: { id: "c1" } }]);
  });

  it("return the answer with the request Rust sent", async () => {
    const backend = createFakeBackend({
      answers: {
        api_tool_call: () => ({
          request: "GET /v1/search?query=archive",
          status: 200,
          body: { hits: [] },
          error: null,
        }),
      },
    });
    const call = { tool: "search", path: {}, query: [["query", "archive"]] };

    const answer = await callTool(backend, "c1", call);

    expect(answer).toEqual({
      request: "GET /v1/search?query=archive",
      status: 200,
      body: { hits: [] },
      error: null,
    });
    expect(backend.calls).toEqual([{ command: "api_tool_call", args: { id: "c1", call } }]);
  });

  it("return the server's refusal of an argument as an error, with the request", async () => {
    const backend = createFakeBackend({
      answers: {
        api_tool_call: () => ({
          request: "GET /v1/search?limit=0",
          status: 422,
          body: null,
          error: {
            kind: "api",
            message: "The request is not valid. Not valid: query.limit (greater_than_equal).",
            status: 422,
            code: "validation_error",
          },
        }),
      },
    });

    const answer = await callTool(backend, "c1", { tool: "search", path: {}, query: [["limit", "0"]] });

    expect(answer.request).toBe("GET /v1/search?limit=0");
    expect(answer.error).toMatchObject({ kind: "api", status: 422, code: "validation_error" });
    expect(answer.body).toBeNull();
  });
});
