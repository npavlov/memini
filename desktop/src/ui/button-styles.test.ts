/// <reference types="node" />
// Buttons that stand in one row share height, padding and font size. That holds because one
// rule, `.button`, sets the size, and no variant or screen overrides it. This test reads the
// stylesheet and the components and fails when a size creeps back in.

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SOURCE = fileURLToPath(new URL("..", import.meta.url));
const styles = readFileSync(`${SOURCE}/styles.css`, "utf8");

/** The properties a rule sets, for a selector written exactly as in the stylesheet. */
function propertiesOf(selector: string): readonly string[] {
  const start = styles.indexOf(`\n${selector} {`);
  const body = start < 0 ? "" : styles.slice(styles.indexOf("{", start) + 1, styles.indexOf("}", start));
  return Array.from(body.matchAll(/^\s*([a-z-]+):/gmu), (match) => match[1] ?? "");
}

function components(): readonly string[] {
  return readdirSync(SOURCE, { recursive: true, encoding: "utf8" })
    .filter((name) => name.endsWith(".tsx"))
    .map((name) => readFileSync(`${SOURCE}/${name}`, "utf8"));
}

const SIZE = ["height", "padding", "font-size", "font", "font-weight", "line-height", "min-height"];

describe("the button styles", () => {
  it("set the size of every button in the one .button rule", () => {
    expect(propertiesOf(".button")).toEqual(expect.arrayContaining(["padding", "font-weight", "line-height"]));
    expect(styles).toContain(".button,\ninput[type=\"text\"],\nselect {\n  height: var(--control-height);");
  });

  it.each([[".button.primary"], [".button.danger"], [".button:disabled"], [".button:hover:not(:disabled)"]])(
    "change only colours in %s, never the size",
    (selector) => {
      const sizes = propertiesOf(selector).filter((property) => SIZE.includes(property));

      expect(propertiesOf(selector).length).toBeGreaterThan(0);
      expect(sizes).toEqual([]);
    },
  );

  it("have no size variant in the stylesheet", () => {
    expect(styles).not.toMatch(/\.button\.(?:small|large|compact)/u);
  });

  it("are used without a size variant by every component", () => {
    const sized = components().filter((text) => /className="button[^"]*\b(?:small|large|compact)\b/u.test(text));

    expect(sized).toEqual([]);
  });

  it("give every button of the app the .button class", () => {
    const bare = components().flatMap((text) =>
      Array.from(text.matchAll(/<button\b[^>]*?className=(?:"([^"]*)"|\{[^}]*\})/gsu), (match) => match[1] ?? "button"),
    );
    const other = bare.filter((name) => !/^(?:button\b|nav-item|tab|row-main)/u.test(name));

    expect(other).toEqual([]);
  });
});
