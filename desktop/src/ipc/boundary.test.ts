/// <reference types="node" />
// The front end's side of the security rules, checked on its own source: it renders text and
// never markup, makes no request of its own, stores nothing, and reaches the Rust side through
// one module.

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

interface Source {
  readonly name: string;
  readonly text: string;
}

const ROOT = fileURLToPath(new URL("..", import.meta.url));

function readSources(): readonly Source[] {
  return readdirSync(ROOT, { recursive: true, encoding: "utf8" })
    .map((name) => name.replaceAll("\\", "/"))
    .filter((name) => /\.tsx?$/u.test(name) && !name.includes(".test."))
    .map((name) => ({ name, text: readFileSync(`${ROOT}/${name}`, "utf8") }));
}

const sources = readSources();

function filesWith(pattern: RegExp): readonly string[] {
  return sources.filter((source) => pattern.test(source.text)).map((source) => source.name);
}

describe("the front end's source", () => {
  it("is read by this test", () => {
    expect(sources.length).toBeGreaterThan(30);
  });

  it.each([
    ["renders markup from a string", /dangerouslySetInnerHTML|\.innerHTML|\.outerHTML|insertAdjacentHTML/u],
    ["writes into the document", /document\.write|DOMParser|createContextualFragment/u],
    ["makes a request of its own", /\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon/u],
    ["evaluates text as code", /\beval\(|new Function\(|\bimport\(/u],
    ["stores something in the webview", /localStorage|sessionStorage|indexedDB|document\.cookie/u],
    ["navigates or opens a link", /href=|window\.open|(?:window|document)\.location/u],
  ])("has no file that %s", (_what, pattern) => {
    expect(filesWith(pattern)).toEqual([]);
  });

  it("reaches Tauri through the backend module only", () => {
    expect(filesWith(/from "@tauri-apps\//u)).toEqual(["ipc/tauri-backend.ts"]);
  });

  it("calls the commands of the Rust side from typed functions, never from a component", () => {
    const components = filesWith(/\.invoke\(/u).filter((name) => name.endsWith(".tsx"));

    expect(components).toEqual([]);
  });

  it("mentions an Authorization header only in the client configuration it prints", () => {
    expect(filesWith(/Authorization/u)).toEqual(["mcp/mcp-clients.ts"]);
  });
});
