import { describe, expect, it } from "vitest";

import { TOKEN_VARIABLE, clientGuides, mcpAddress, mcpAddressOf, shellQuote } from "./mcp-clients";
import type { ClientGuide } from "./mcp-clients";

const LOCAL = "http://127.0.0.1:6364/mcp";

function guide(id: string, url: string = LOCAL): ClientGuide | undefined {
  return clientGuides(url).find((item) => item.id === id);
}

function allText(url: string): string {
  return clientGuides(url)
    .flatMap((item) => item.snippets.map((snippet) => snippet.text))
    .join("\n");
}

describe("mcpAddress", () => {
  it.each([
    ["http://127.0.0.1:6364/mcp", "http://127.0.0.1:6364/mcp"],
    [" https://memini.example.org/prefix/mcp ", "https://memini.example.org/prefix/mcp"],
    ["https://memini.example.org/a b/mcp", "https://memini.example.org/a%20b/mcp"],
  ])("accepts %s as an address for the snippets", (text, address) => {
    expect(mcpAddress(text)).toBe(address);
  });

  it.each([
    ["no address at all", "memini"],
    ["another scheme", "javascript:alert(1)"],
    ["a file", "file:///etc/hosts"],
    ["credentials", "https://sam:pw@memini.example.org/mcp"],
    ["a fragment", "https://memini.example.org/mcp#x"],
  ])("returns null for %s", (_what, text) => {
    expect(mcpAddress(text)).toBeNull();
  });
});

describe("mcpAddressOf", () => {
  it.each([
    ["http://127.0.0.1:6364", "http://127.0.0.1:6364/mcp"],
    ["https://memini.example.org/", "https://memini.example.org/mcp"],
    ["https://memini.example.org/prefix", "https://memini.example.org/prefix/mcp"],
  ])("appends the MCP path to the server address %s", (base, address) => {
    expect(mcpAddressOf(base)).toBe(address);
  });
});

describe("shellQuote", () => {
  it("leaves an argument of plain characters as it is", () => {
    expect(shellQuote(LOCAL)).toBe(LOCAL);
  });

  it("puts an argument with shell characters into single quotes", () => {
    expect(shellQuote("https://memini.example.org/a;b $(c)")).toBe(
      "'https://memini.example.org/a;b $(c)'",
    );
  });

  it("keeps a single quote inside the argument an ordinary character", () => {
    expect(shellQuote("it's")).toBe("'it'\\''s'");
  });
});

describe("clientGuides", () => {
  it("covers the clients of the documentation, each with something to copy", () => {
    const guides = clientGuides(LOCAL);

    expect(guides.map((item) => item.id)).toEqual([
      "claude-code",
      "codex",
      "cursor",
      "vscode-copilot",
      "gemini",
      "windsurf",
      "zed",
      "cline",
      "opencode",
      "pi",
      "other",
    ]);
    expect(guides.every((item) => item.snippets.length > 0)).toBe(true);
  });

  it("writes the Claude Code command with the JSON in single quotes", () => {
    const command = guide("claude-code")?.snippets[0]?.text;

    expect(command).toBe(
      `claude mcp add-json --scope local memini '{"type":"http","url":"${LOCAL}","headers":{"Authorization":"Bearer \${${TOKEN_VARIABLE}}"}}'`,
    );
  });

  it("writes the Codex command with the name of the token's variable", () => {
    const command = guide("codex")?.snippets[0]?.text;

    expect(command).toBe(
      `codex mcp add memini --url ${LOCAL} --bearer-token-env-var ${TOKEN_VARIABLE}\ncodex mcp get memini`,
    );
  });

  it("puts the address of the connection into every snippet", () => {
    const address = "https://memini.example.org/prefix/mcp";

    const texts = clientGuides(address).flatMap((item) => item.snippets.map((s) => s.text));

    expect(texts.every((text) => text.includes(address))).toBe(true);
  });

  it("keeps an address with a single quote inside the quotes of the shell command", () => {
    const address = "https://memini.example.org/it's/mcp";

    const command = guide("claude-code", address)?.snippets[0]?.text;

    expect(command).toContain("it'\\''s");
  });

  it("keeps an address with a double quote inside the JSON string", () => {
    const address = 'https://memini.example.org/a"b/mcp';

    const text = guide("cursor", address)?.snippets[0]?.text ?? "";

    expect(JSON.parse(text)).toEqual({
      mcpServers: {
        memini: { url: address, headers: { Authorization: "Bearer ${env:MEMINI_READ_TOKEN}" } },
      },
    });
  });

  it("never writes a token: only the variable or a placeholder follows Bearer", () => {
    const afterBearer = Array.from(allText(LOCAL).matchAll(/Bearer ([^"\n]*)/gu), (m) => m[1]);

    expect(new Set(afterBearer)).toEqual(
      new Set([
        "${MEMINI_READ_TOKEN}",
        "${env:MEMINI_READ_TOKEN}",
        "{env:MEMINI_READ_TOKEN}",
        "<workspace-read-token>",
        "<the read token, from the client's secret input or an environment variable>",
      ]),
    );
  });
});
