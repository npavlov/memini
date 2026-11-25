// Ready configuration for the common MCP clients, following docs/mcp of the repository. Each
// snippet names the environment variable that holds the read token; none contains a token.
// JSON is written by the JSON encoder and shell arguments are quoted, so an unusual address
// stays an address.

/** The environment variable the snippets read the token from, as in docs/mcp. */
export const TOKEN_VARIABLE = "MEMINI_READ_TOKEN";

/** The file the documentation keeps the read token in. */
const TOKEN_FILE = "~/.config/memini/mcp.token";

const SERVER_NAME = "memini";

export type SnippetLanguage = "sh" | "powershell" | "json" | "text";

export interface Snippet {
  /** What the snippet is or where it goes. */
  readonly label: string;
  readonly language: SnippetLanguage;
  readonly text: string;
}

export interface ClientGuide {
  readonly id: string;
  readonly name: string;
  readonly snippets: readonly Snippet[];
  /** How to see that the client is connected. */
  readonly check: string;
}

/** The MCP address as the snippets may carry it, or null when the text is not an http(s) address. */
export function mcpAddress(text: string): string | null {
  let url: URL;
  try {
    url = new URL(text.trim());
  } catch (error) {
    if (error instanceof TypeError) {
      return null;
    }
    throw error;
  }
  const plain = url.username === "" && url.password === "" && url.hash === "";
  const web = url.protocol === "http:" || url.protocol === "https:";
  return plain && web ? url.href : null;
}

/** The MCP endpoint of a server, from the server's base address. */
export function mcpAddressOf(baseUrl: string): string | null {
  return mcpAddress(`${baseUrl.trim().replace(/\/+$/u, "")}/mcp`);
}

/** An argument for a POSIX shell: as it is when every character is plain, else single-quoted. */
export function shellQuote(text: string): string {
  if (/^[A-Za-z0-9_\-.,:/@%+=]+$/u.test(text)) {
    return text;
  }
  return `'${text.replaceAll("'", "'\\''")}'`;
}

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function bearer(reference: string): { readonly Authorization: string } {
  return { Authorization: `Bearer ${reference}` };
}

function servers(key: string, entry: unknown): string {
  return json({ [key]: { [SERVER_NAME]: entry } });
}

/** Loading the token into the shell that starts the client, as docs/mcp does it. */
export function tokenSetup(): readonly Snippet[] {
  return [
    {
      label: "Bash or Zsh, in the shell that starts the client",
      language: "sh",
      text: `export ${TOKEN_VARIABLE}="$(cat ${TOKEN_FILE})"`,
    },
    {
      label: "PowerShell, in the window that starts the client",
      language: "powershell",
      text: `$env:${TOKEN_VARIABLE} = (Get-Content -Raw (Join-Path $HOME '.config/memini/mcp.token')).Trim()`,
    },
  ];
}

function claudeCode(url: string): ClientGuide {
  const entry = { type: "http", url, headers: bearer(`\${${TOKEN_VARIABLE}}`) };
  return {
    id: "claude-code",
    name: "Claude Code",
    snippets: [
      {
        label: "For yourself in this project: run once. Keep the single quotes.",
        language: "sh",
        text: `claude mcp add-json --scope local ${SERVER_NAME} ${shellQuote(JSON.stringify(entry))}`,
      },
      {
        label: "For the whole project: merge into .mcp.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Start claude from the prepared shell, open /mcp and look for memini.",
  };
}

function codex(url: string): ClientGuide {
  return {
    id: "codex",
    name: "Codex CLI",
    snippets: [
      {
        label: "Run once",
        language: "sh",
        text: `codex mcp add ${SERVER_NAME} --url ${shellQuote(url)} --bearer-token-env-var ${TOKEN_VARIABLE}\ncodex mcp get ${SERVER_NAME}`,
      },
    ],
    check: "Start codex from the prepared shell and open /mcp.",
  };
}

function cursor(url: string): ClientGuide {
  const entry = { url, headers: bearer(`\${env:${TOKEN_VARIABLE}}`) };
  return {
    id: "cursor",
    name: "Cursor",
    snippets: [
      {
        label: "Merge into ~/.cursor/mcp.json or the project's .cursor/mcp.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Open Cursor's MCP settings and enable memini.",
  };
}

function copilot(url: string): ClientGuide {
  const entry = { type: "http", url, headers: bearer(`\${${TOKEN_VARIABLE}}`), tools: ["*"] };
  return {
    id: "vscode-copilot",
    name: "VS Code with GitHub Copilot",
    snippets: [
      {
        label: "Copilot harness: merge into ~/.copilot/mcp-config.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Run Chat: Open Customizations, select MCP Servers and start memini.",
  };
}

function gemini(url: string): ClientGuide {
  const entry = { httpUrl: url, headers: bearer(`\${${TOKEN_VARIABLE}}`) };
  return {
    id: "gemini",
    name: "Gemini CLI",
    snippets: [
      {
        label: "Merge into ~/.gemini/settings.json or the project's .gemini/settings.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Run gemini mcp list and look for memini marked connected.",
  };
}

function windsurf(url: string): ClientGuide {
  const entry = { serverUrl: url, headers: bearer(`\${env:${TOKEN_VARIABLE}}`) };
  return {
    id: "windsurf",
    name: "Windsurf",
    snippets: [
      {
        label: "Cascade: merge into ~/.codeium/windsurf/mcp_config.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Refresh Cascade's MCP server list.",
  };
}

function zed(url: string): ClientGuide {
  const entry = { url, headers: bearer("<workspace-read-token>") };
  return {
    id: "zed",
    name: "Zed",
    snippets: [
      {
        label:
          "Zed takes the header as written: merge into your personal settings.json and replace the placeholder there, never in a project file",
        language: "json",
        text: servers("context_servers", entry),
      },
    ],
    check: "Open Settings, AI, MCP Servers: memini should say the server is active.",
  };
}

function cline(url: string): ClientGuide {
  const entry = { type: "streamableHttp", url, headers: bearer(`\${env:${TOKEN_VARIABLE}}`) };
  return {
    id: "cline",
    name: "Cline",
    snippets: [
      {
        label: "MCP Servers, Configure: merge into cline_mcp_settings.json",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "In MCP Servers, expand memini and look for the status tool.",
  };
}

function openCode(url: string): ClientGuide {
  const entry = {
    type: "remote",
    url,
    headers: bearer(`{env:${TOKEN_VARIABLE}}`),
    oauth: false,
  };
  return {
    id: "opencode",
    name: "OpenCode",
    snippets: [
      {
        label: "OpenCode 1.x: merge into the project's opencode.jsonc",
        language: "json",
        text: json({ mcp: { [SERVER_NAME]: entry } }),
      },
      {
        label: "OpenCode 2.x: merge into the project's opencode.jsonc",
        language: "json",
        text: json({ mcp: { servers: { [SERVER_NAME]: entry } } }),
      },
    ],
    check: "Run opencode mcp list and look for memini marked connected.",
  };
}

function pi(url: string): ClientGuide {
  const entry = { url, headers: bearer(`\${${TOKEN_VARIABLE}}`) };
  return {
    id: "pi",
    name: "Pi coding agent",
    snippets: [
      {
        label: "Merge into ~/.pi/agent/mcp.json, or .pi/mcp.json in a trusted project",
        language: "json",
        text: servers("mcpServers", entry),
      },
    ],
    check: "Run pi mcp list and look for memini: connected.",
  };
}

function other(url: string): ClientGuide {
  return {
    id: "other",
    name: "Other clients",
    snippets: [
      {
        label: "Add a server over Streamable HTTP with these values",
        language: "text",
        text: `Name: ${SERVER_NAME}\nTransport: Streamable HTTP\nURL: ${url}\nHeader: Authorization: Bearer <the read token, from the client's secret input or an environment variable>`,
      },
    ],
    check: "Ask the agent to call the Memini status tool.",
  };
}

/** The guides for an MCP address that `mcpAddress` accepted. */
export function clientGuides(url: string): readonly ClientGuide[] {
  return [
    claudeCode(url),
    codex(url),
    cursor(url),
    copilot(url),
    gemini(url),
    windsurf(url),
    zed(url),
    cline(url),
    openCode(url),
    pi(url),
    other(url),
  ];
}
