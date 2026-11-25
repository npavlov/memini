/// <reference types="node" />
// The app's client configuration is taken from docs/mcp of the repository. These tests read the
// documentation and fail when the two drift apart: change both together.

import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { clientGuides, tokenSetup } from "./mcp-clients";

const PAGES = ["README.md", "clients/editors.md", "clients/more.md", "clients/terminal.md"];
/** The address every example of the documentation uses. */
const DOCUMENTED = "http://127.0.0.1:6364/mcp";

function readDocumentation(): string {
  return PAGES.map((page) =>
    readFileSync(new URL(`../../../docs/mcp/${page}`, import.meta.url), "utf8"),
  ).join("\n");
}

function jsonBlocks(markdown: string): readonly unknown[] {
  return Array.from(markdown.matchAll(/```json\n([\s\S]*?)```/gu), (match): unknown =>
    JSON.parse(match[1] ?? "null"),
  );
}

const documentation = readDocumentation();
const documented = jsonBlocks(documentation);
const snippets = clientGuides(DOCUMENTED).flatMap((guide) =>
  guide.snippets.map((snippet) => ({ client: guide.name, ...snippet })),
);

describe("the client configuration against docs/mcp", () => {
  it.each(snippets.filter((snippet) => snippet.language === "json"))(
    "matches a JSON example of the documentation: $client, $label",
    (snippet) => {
      expect(documented).toContainEqual(JSON.parse(snippet.text));
    },
  );

  it.each(
    snippets
      .filter((snippet) => snippet.language === "sh")
      .flatMap((snippet) => snippet.text.split("\n")),
  )("is a command of the documentation: %s", (line) => {
    expect(documentation).toContain(line);
  });

  it.each(tokenSetup().map((snippet) => snippet.text))(
    "loads the token as the documentation does: %s",
    (line) => {
      expect(documentation).toContain(line);
    },
  );
});
