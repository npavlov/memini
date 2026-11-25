/// <reference types="node" />
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { Glyph, Icon, readGlyphDrawing } from "./icons";
import { markupOf } from "./markup";

const DESKTOP = fileURLToPath(new URL("../..", import.meta.url));
const source = readFileSync(`${DESKTOP}/brand-src/glyph.svg`, "utf8");
const drawing = readGlyphDrawing(source);

describe("readGlyphDrawing", () => {
  it("finds the line and the point of the brand's glyph", () => {
    expect(drawing.path.startsWith("M")).toBe(true);
    expect(Number(drawing.dotRadius)).toBeGreaterThan(0);
  });

  it("reads a stroked line with its paint", () => {
    const stroked = readGlyphDrawing(
      '<svg><path fill="none" stroke="currentColor" stroke-width="3.3" stroke-linecap="round" d="M3 18 V6"/><circle fill="currentColor" cx="1" cy="2" r="3"/></svg>',
    );

    expect(stroked).toEqual({
      path: "M3 18 V6",
      paint: {
        fill: "none",
        stroke: "currentColor",
        strokeWidth: "3.3",
        strokeLinecap: "round",
        strokeLinejoin: undefined,
      },
      dotX: "1",
      dotY: "2",
      dotRadius: "3",
      dotFill: "currentColor",
    });
  });

  it("reads a filled shape without inventing a stroke", () => {
    const filled = readGlyphDrawing('<svg><path fill="currentColor" d="M1 1 Z"/><circle cx="1" cy="2" r="3"/></svg>');

    expect(filled.paint).toEqual({
      fill: "currentColor",
      stroke: undefined,
      strokeWidth: undefined,
      strokeLinecap: undefined,
      strokeLinejoin: undefined,
    });
  });
});

describe("Glyph", () => {
  it("draws the path of brand-src/glyph.svg as it is on disk", () => {
    expect(markupOf(<Glyph />)).toContain(`d="${drawing.path}"`);
  });

  it("is one colour, the colour of the text around it", () => {
    const markup = markupOf(<Glyph />);
    const paints = Array.from(markup.matchAll(/(?:fill|stroke)="([^"]*)"/gu), (match) => match[1]);

    expect(new Set(paints).has("currentColor")).toBe(true);
    expect(paints.filter((paint) => paint !== "currentColor" && paint !== "none")).toEqual([]);
  });

  it("is decoration beside the name: hidden from a screen reader, in the size it is given", () => {
    const markup = markupOf(<Glyph size={22} />);

    expect(markup).toContain('aria-hidden="true"');
    expect(markup).toContain('width="22" height="22"');
  });
});

describe("Icon", () => {
  it.each(["refresh", "chevron-up", "chevron-down"] as const)("draws %s in the colour of its text", (name) => {
    const markup = markupOf(<Icon name={name} />);

    expect(markup).toContain('stroke="currentColor"');
    expect(markup).toContain('aria-hidden="true"');
  });
});

describe("the app's icon set", () => {
  it("holds the files the bundle names, generated from brand-src/icon.svg", () => {
    const icons = readdirSync(`${DESKTOP}/src-tauri/icons`).sort();

    expect(icons).toEqual(["128x128.png", "128x128@2x.png", "32x32.png", "icon.icns", "icon.ico", "icon.png"]);
  });
});
