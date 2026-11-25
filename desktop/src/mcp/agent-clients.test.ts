import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import {
  connectClient,
  connectNote,
  connectProblem,
  describeClient,
  describeDisconnected,
  disconnectClient,
  listAgentClients,
  literalQuestion,
  loadSnippets,
  needsVariant,
  parseAgentClient,
  parseDisconnected,
  toConnectRequest,
  writesLiteral,
} from "./agent-clients";
import type { AgentClient, ClientStatus, ConnectChoice, Revoked } from "./agent-clients";

const ADDRESS = "http://127.0.0.1:6364/mcp";

const answer = {
  id: "cursor",
  name: "Cursor",
  file: "/home/dana/.cursor/mcp.json",
  state: "connected",
  url: ADDRESS,
  token: "reference",
  variable: "MEMINI_READ_TOKEN_CURSOR",
  takes_literal: true,
  token_file: "/home/dana/.config/memini/clients/cursor.token",
  has_token_file: true,
  variants: [],
  variant: null,
  has_token: true,
};

const cursor: AgentClient = {
  id: "cursor",
  name: "Cursor",
  file: "/home/dana/.cursor/mcp.json",
  status: { state: "connected", url: ADDRESS, token: "reference" },
  variable: "MEMINI_READ_TOKEN_CURSOR",
  takesLiteral: true,
  tokenFile: "/home/dana/.config/memini/clients/cursor.token",
  hasTokenFile: true,
  variants: [],
  variant: null,
  hasToken: true,
};

const zed: AgentClient = { ...cursor, id: "zed", name: "Zed", variable: null, hasTokenFile: false };

const codex: AgentClient = { ...cursor, id: "codex", name: "Codex CLI", takesLiteral: false };

const opencode: AgentClient = {
  ...cursor,
  id: "opencode",
  name: "OpenCode",
  variants: [
    { id: "1", label: "OpenCode 1.x" },
    { id: "2", label: "OpenCode 2.x" },
  ],
};

const byVariable: ConnectChoice = { workspace: "", literal: false, variant: "" };
const inFile: ConnectChoice = { workspace: "", literal: true, variant: "" };

function withStatus(status: ClientStatus): AgentClient {
  return { ...cursor, status };
}

describe("parseAgentClient", () => {
  it("reads a connected client as its file shows it", () => {
    const client = parseAgentClient(answer);

    expect(client).toEqual(cursor);
  });

  it.each([
    [{ state: "absent" }, { state: "absent" }],
    [{ state: "unreadable", message: "Not valid." }, { state: "unreadable", message: "Not valid." }],
    [{ state: "something new" }, { state: "absent" }],
    [{ state: "connected", url: 7, token: "odd" }, { state: "connected", url: null, token: "none" }],
  ])("reads the state %j as %j", (state, expected) => {
    const client = parseAgentClient({ ...answer, url: undefined, token: undefined, ...state });

    expect(client.status).toEqual(expected);
  });

  it("refuses an answer that is not a client", () => {
    const parse = (): AgentClient => parseAgentClient("cursor");

    expect(parse).toThrow("The answer is not an agent client.");
  });
});

describe("writesLiteral", () => {
  it.each([
    ["a client with a variable, by default", cursor, byVariable, false],
    ["a client with a variable, when the user asks", cursor, inFile, true],
    ["a client without a variable, whatever is asked", zed, byVariable, true],
    ["a client the app writes no token for", codex, inFile, false],
  ])("for %s", (_case, client, choice, expected) => {
    const literal = writesLiteral(client, choice);

    expect(literal).toBe(expected);
  });
});

describe("connectProblem", () => {
  it("asks for the version when the file of the client does not tell it", () => {
    const problem = connectProblem(opencode, byVariable);

    expect(problem).toBe("Choose the version of OpenCode: its file does not tell.");
  });

  it.each([
    ["the version was chosen", opencode, { ...byVariable, variant: "2" }],
    ["the file tells the version", { ...opencode, variant: "1" }, byVariable],
    ["the client has one layout", cursor, byVariable],
  ])("has nothing to say when %s", (_case, client, choice) => {
    const problem = connectProblem(client, choice);

    expect(problem).toBeNull();
  });

  it("needs no version from a client whose file tells it", () => {
    const needed = needsVariant({ ...opencode, variant: "1" });

    expect(needed).toBe(false);
  });
});

describe("what the app says before Connect", () => {
  it("names the file a token is written into", () => {
    const question = literalQuestion(cursor);

    expect(question).toBe(
      "The token will be in /home/dana/.cursor/mcp.json as plain text: whoever can read that file can read what the token reads. Write it?",
    );
  });

  it("says that a client without a variable takes a token only", () => {
    const question = literalQuestion(zed);

    expect(question).toContain("Zed takes only a token written into its file. ");
  });

  it.each([
    [cursor, byVariable, "writes an entry that reads MEMINI_READ_TOKEN_CURSOR"],
    [cursor, inFile, "writes it into the client’s file"],
    [zed, byVariable, "writes it into the client’s file"],
  ])("says how %o gets its token with %o", (client, choice, expected) => {
    const note = connectNote(client, choice);

    expect(note).toContain(expected);
  });
});

describe("describeClient", () => {
  it.each<[ClientStatus, string, string]>([
    [{ state: "absent" }, "idle", "Not connected"],
    [{ state: "unreadable", message: "Not valid." }, "bad", "Not readable"],
    [{ state: "connected", url: ADDRESS, token: "reference" }, "good", "Connected"],
    [{ state: "connected", url: ADDRESS, token: "literal" }, "good", "Connected"],
    [{ state: "connected", url: ADDRESS, token: "none" }, "warn", "Connected without a token"],
    [{ state: "connected", url: "https://other.example/mcp", token: "reference" }, "warn", "Connected to another address"],
    [{ state: "connected", url: null, token: "reference" }, "warn", "Connected to another address"],
  ])("describes %j as %s: %s", (status, tone, label) => {
    const summary = describeClient(withStatus(status), ADDRESS);

    expect([summary.tone, summary.label]).toEqual([tone, label]);
  });

  it("shows the address of an entry that is for another server", () => {
    const client = withStatus({ state: "connected", url: "https://other.example/mcp", token: "literal" });

    const summary = describeClient(client, ADDRESS);

    expect(summary.text).toBe("https://other.example/mcp");
  });
});

describe("loadSnippets", () => {
  it("gives the lines that load the token file into the variable of the client", () => {
    const snippets = loadSnippets({ ...cursor, tokenFile: "/home/dana lee/.config/memini/clients/cursor.token" });

    expect(snippets.map((snippet) => snippet.text)).toEqual([
      "export MEMINI_READ_TOKEN_CURSOR=\"$(cat '/home/dana lee/.config/memini/clients/cursor.token')\"",
      "$env:MEMINI_READ_TOKEN_CURSOR = (Get-Content -Raw '/home/dana lee/.config/memini/clients/cursor.token').Trim()",
    ]);
  });

  it.each([
    ["has no variable", zed],
    ["has no token file", { ...cursor, hasTokenFile: false }],
  ])("gives nothing for a client that %s", (_case, client) => {
    const snippets = loadSnippets(client);

    expect(snippets).toEqual([]);
  });
});

describe("describeDisconnected", () => {
  it.each<[Revoked, boolean, string, string]>([
    [{ result: "done" }, true, "good", "The entry was removed, and its token is revoked."],
    [{ result: "nothing" }, false, "info", "The file had no entry. The app made no token for it, so none was revoked: if the entry held a token, revoke it on the Tokens screen."],
    [{ result: "failed", message: "The server cannot be reached." }, true, "warn", "The entry was removed, but its token could not be revoked. The server cannot be reached."],
  ])("says what became of the entry and the token for %j", (revoked, removed, tone, text) => {
    const summary = describeDisconnected({ client: cursor, removed, revoked });

    expect([summary.tone, summary.text]).toEqual([tone, text]);
  });

  it("reads the answer to Disconnect", () => {
    const outcome = parseDisconnected({ client: answer, removed: true, revoked: { result: "failed", message: "No." } });

    expect(outcome).toEqual({ client: cursor, removed: true, revoked: { result: "failed", message: "No." } });
  });
});

describe("the commands of the Rust side", () => {
  it("asks Connect for a client by its id, with the choice of the user", () => {
    const request = toConnectRequest({ client: cursor, connection: "c1" }, { workspace: "harbor", literal: false, variant: "" });

    expect(request).toEqual({ client: "cursor", connection: "c1", workspace: "harbor", literal: false, variant: null });
  });

  it("asks for the token in the file for a client that has no variable", () => {
    const request = toConnectRequest({ client: zed, connection: "c1" }, byVariable);

    expect(request).toEqual({ client: "zed", connection: "c1", workspace: null, literal: true, variant: null });
  });

  it("lists the clients the Rust side found", async () => {
    const backend = createFakeBackend({ answers: { clients_list: () => [answer] } });

    const clients = await listAgentClients(backend);

    expect(clients).toEqual([cursor]);
  });

  it("connects through one command and gets the client back, never a token", async () => {
    const backend = createFakeBackend({ answers: { client_connect: () => answer } });

    const client = await connectClient(backend, { client: cursor, connection: "c1" }, byVariable);

    expect([client, backend.calls]).toEqual([
      cursor,
      [
        {
          command: "client_connect",
          args: { request: { client: "cursor", connection: "c1", workspace: null, literal: false, variant: null } },
        },
      ],
    ]);
  });

  it("disconnects a client by its id", async () => {
    const backend = createFakeBackend({
      answers: { client_disconnect: () => ({ client: answer, removed: true, revoked: { result: "done" } }) },
    });

    const outcome = await disconnectClient(backend, "cursor");

    expect([outcome.revoked, backend.calls]).toEqual([
      { result: "done" },
      [{ command: "client_disconnect", args: { client: "cursor" } }],
    ]);
  });
});
