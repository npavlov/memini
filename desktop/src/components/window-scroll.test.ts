/// <reference types="node" />
// The window never scrolls as a whole: only the screen in <main> does. A document that can
// scroll lets a dragged selection or a focused element slide the side bar and the status bar out
// of the window, as the owner saw on the Repositories screen.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const css = readFileSync(fileURLToPath(new URL("./components.css", import.meta.url)), "utf8");

function rule(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start));
}

describe("the window's scrolling", () => {
  it("clips the document, so only the screen scrolls", () => {
    expect(rule("html,\nbody")).toContain("overflow: clip;");
  });

  it("makes the screen the containing block of what is positioned in it", () => {
    const main = rule(".app-frame > main");
    expect(main).toContain("position: relative;");
    expect(main).toContain("overflow: auto;");
  });
});
