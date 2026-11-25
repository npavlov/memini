/// <reference types="node" />
// The rule of the component library, checked on the source: screens are built from the library
// and hold no raw control and no styling of their own. When this test fails, fix the screen:
// use a component, extend one, or add one to src/components/ (see "Components" in the README).

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

interface Source {
  readonly name: string;
  readonly text: string;
}

const ROOT = fileURLToPath(new URL("..", import.meta.url));

function readAll(): readonly Source[] {
  return readdirSync(ROOT, { recursive: true, encoding: "utf8" })
    .map((name) => name.replaceAll("\\", "/"))
    .filter((name) => /\.(?:tsx?|css)$/u.test(name))
    .map((name) => ({ name, text: readFileSync(`${ROOT}/${name}`, "utf8") }));
}

const all = readAll();
const library = all.filter((source) => source.name.startsWith("components/"));
/** Everything a screen is made of: every file outside the library, tests left out. */
const screens = all.filter((source) => !source.name.startsWith("components/") && !source.name.includes(".test."));

function screensWith(pattern: RegExp): readonly string[] {
  return screens.filter((source) => pattern.test(source.text)).map((source) => source.name);
}

describe("a file outside src/components", () => {
  it("is read by this test", () => {
    expect(screens.length).toBeGreaterThan(40);
  });

  it.each([
    ["button", /<button\b/u],
    ["input", /<input\b/u],
    ["select", /<select\b/u],
    ["textarea", /<textarea\b/u],
    ["table", /<table\b/u],
    ["label", /<label\b/u],
    ["fieldset", /<fieldset\b/u],
    ["form", /<form\b/u],
    ["pre", /<pre\b/u],
    ["svg", /<svg\b/u],
  ])("holds no raw <%s>: the library has a component for it", (_element, pattern) => {
    expect(screensWith(pattern)).toEqual([]);
  });

  it.each([
    ["a class name", /\bclassName=/u],
    ["an inline style", /\bstyle=\{/u],
  ])("has no styling of its own: no %s", (_what, pattern) => {
    expect(screensWith(pattern)).toEqual([]);
  });

  it("is never a style sheet", () => {
    expect(screens.filter((source) => source.name.endsWith(".css")).map((source) => source.name)).toEqual([]);
  });

  it("imports components through the library's entry point only", () => {
    const deep = /from "(?:\.\.?\/)+components\/[^"]+"/u;

    expect(screensWith(deep)).toEqual([]);
  });

  it("loads the library's style sheets in the entry file and nowhere else", () => {
    expect(screensWith(/import "[^"]+\.css"/u)).toEqual(["main.tsx"]);
  });
});

describe("the library's entry point", () => {
  const index = library.find((source) => source.name === "components/index.ts")?.text ?? "";

  it.each(
    library
      .filter((source) => source.name.endsWith(".tsx") && !source.name.includes(".test."))
      .flatMap((source) =>
        Array.from(source.text.matchAll(/^export function ([A-Z]\w*)/gmu), (match) => match[1] ?? ""),
      ),
  )("exports the component %s", (component) => {
    expect(index).toMatch(new RegExp(`\\b${component}\\b`, "u"));
  });

  it.each(
    library
      .filter((source) => source.name.endsWith(".tsx") && !source.name.includes(".test."))
      .map((source) => source.name),
  )("has a test beside %s", (name) => {
    const test = name.replace(/\.tsx$/u, ".test.tsx");

    expect(library.map((source) => source.name)).toContain(test);
  });
});
