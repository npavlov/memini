/// <reference types="node" />
// The tokens are the only place a colour or a radius is written, and every text colour is
// readable on the ground it stands on: a contrast of at least 4.5 to 1 in both themes.

import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const SOURCE = fileURLToPath(new URL("..", import.meta.url));
const tokens = readFileSync(`${SOURCE}/components/tokens.css`, "utf8");

type Rgb = readonly [number, number, number];
type Theme = ReadonlyMap<string, string>;

const DARK_START = tokens.indexOf("@media (prefers-color-scheme: dark)");

function declarations(css: string): Theme {
  return new Map(Array.from(css.matchAll(/(--[a-z-]+):\s*([^;]+);/gu), (match) => [match[1] ?? "", (match[2] ?? "").trim()]));
}

const light = declarations(tokens.slice(0, DARK_START));
const dark: Theme = new Map([...light, ...declarations(tokens.slice(DARK_START))]);

function hex(value: string): Rgb {
  const digits = /^#([0-9a-f]{6})$/iu.exec(value)?.[1] ?? "";
  const channel = (at: number): number => Number.parseInt(digits.slice(at, at + 2), 16);
  return [channel(0), channel(2), channel(4)];
}

/** A translucent colour laid over a ground, as the active item lies on the sidebar. */
function over(value: string, ground: Rgb): Rgb {
  const parts = Array.from(value.matchAll(/[\d.]+/gu), (match) => Number(match[0]));
  const alpha = parts[3] ?? 1;
  const mix = (at: 0 | 1 | 2): number => (parts[at] ?? 0) * alpha + ground[at] * (1 - alpha);
  return [mix(0), mix(1), mix(2)];
}

function luminance(colour: Rgb): number {
  const linear = (value: number): number => {
    const share = value / 255;
    return share <= 0.03928 ? share / 12.92 : ((share + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * linear(colour[0]) + 0.7152 * linear(colour[1]) + 0.0722 * linear(colour[2]);
}

function contrast(text: Rgb, ground: Rgb): number {
  const [lighter, darker] = [luminance(text), luminance(ground)].sort((a, b) => b - a);
  return ((lighter ?? 0) + 0.05) / ((darker ?? 0) + 0.05);
}

function colour(theme: Theme, name: string): Rgb {
  return hex(theme.get(name) ?? "");
}

/** Every text colour with each ground it is written on. */
const PAIRS = [
  ["--text", "--ground"],
  ["--text", "--surface"],
  ["--text", "--raised"],
  ["--text", "--control-ground"],
  ["--muted", "--ground"],
  ["--muted", "--surface"],
  ["--muted", "--raised"],
  ["--muted", "--idle-soft"],
  ["--accent", "--ground"],
  ["--accent", "--raised"],
  ["--accent", "--accent-soft"],
  ["--on-accent", "--accent"],
  ["--on-accent", "--accent-hover"],
  ["--on-accent", "--accent-pressed"],
  ["--aqua", "--raised"],
  ["--aqua", "--aqua-soft"],
  ["--danger", "--raised"],
  ["--danger", "--control-ground"],
  ["--danger", "--danger-soft"],
  ["--warn", "--raised"],
  ["--warn", "--warn-soft"],
  ["--text", "--accent-soft"],
  ["--text", "--aqua-soft"],
  ["--text", "--danger-soft"],
  ["--text", "--warn-soft"],
  ["--text", "--idle-soft"],
  ["--sidebar-text", "--sidebar-top"],
  ["--sidebar-text", "--sidebar-bottom"],
  ["--sidebar-muted", "--sidebar-top"],
  ["--sidebar-muted", "--sidebar-bottom"],
] as const;

const THEMES = [
  ["light", light],
  ["dark", dark],
] as const;

const CASES = THEMES.flatMap(([name, theme]) => PAIRS.map(([text, ground]) => [name, text, ground, theme] as const));

function styleSheets(): readonly (readonly [string, string])[] {
  return readdirSync(SOURCE, { recursive: true, encoding: "utf8" })
    .filter((name) => /\.(?:css|tsx?)$/u.test(name) && !/tokens\.(?:css|test\.ts)$/u.test(name))
    .map((name) => [name.replaceAll("\\", "/"), readFileSync(`${SOURCE}/${name}`, "utf8")] as const);
}

function filesWith(pattern: RegExp): readonly string[] {
  return styleSheets()
    .filter(([name, text]) => (name.endsWith(".css") || !name.includes(".test.")) && pattern.test(text))
    .map(([name]) => name);
}

describe("the contrast of the tokens", () => {
  it.each(CASES)("is at least 4.5 to 1 in the %s theme for %s on %s", (_theme, text, ground, theme) => {
    expect(contrast(colour(theme, text), colour(theme, ground))).toBeGreaterThanOrEqual(4.5);
  });

  it.each(THEMES)("is at least 4.5 to 1 in the %s theme for the active item of the sidebar", (_name, theme) => {
    const ground = over(theme.get("--sidebar-active") ?? "", colour(theme, "--sidebar-top"));

    expect(contrast(colour(theme, "--sidebar-text"), ground)).toBeGreaterThanOrEqual(4.5);
  });
});

describe("the tokens file", () => {
  it.each([
    ["--ground", "#f5f7fb", "#0d1322"],
    ["--surface", "#e8edf5", "#161f35"],
    ["--raised", "#ffffff", "#1b2742"],
    ["--text", "#121b2e", "#e8edf7"],
    ["--muted", "#4a5870", "#9aa7bd"],
    ["--line", "#c8d1df", "#2a3650"],
    ["--accent", "#2450c8", "#8fb0ff"],
    ["--aqua", "#0c7366", "#5fe0c8"],
    ["--danger", "#b3261e", "#ff8a80"],
  ])("holds the brand's %s in the light and the dark theme", (name, inLight, inDark) => {
    expect([light.get(name), dark.get(name)]).toEqual([inLight, inDark]);
  });

  it("keeps the sidebar navy in both themes", () => {
    const sidebar = ["--sidebar-top", "--sidebar-bottom", "--sidebar-text", "--sidebar-muted"];

    expect(sidebar.map((name) => dark.get(name))).toEqual(["#1b2f63", "#0f1a38", "#e8edf7", "#c5cfe2"]);
  });

  it.each([
    ["--radius-control", "10px"],
    ["--radius-panel", "12px"],
    ["--radius-chip", "999px"],
  ])("sets the shape %s to %s", (name, value) => {
    expect(light.get(name)).toBe(value);
  });

  it("gives the primary button one small shadow and no glow", () => {
    expect(light.get("--shadow-primary")).toBe("0 1px 2px rgba(18, 27, 46, 0.25)");
  });

  it("gives the secondary button the brand's border", () => {
    expect([light.get("--control-ground"), light.get("--control-line")]).toEqual(["#ffffff", "#aeb9cb"]);
  });
});

describe("the other files of the front end", () => {
  it.each([
    ["a colour in hex", /#[0-9a-f]{3,8}\b(?![\w-])/iu],
    ["a colour by function", /\b(?:rgba?|hsla?|oklch|color-mix)\(/u],
  ])("write no %s: colours come from the tokens", (_what, pattern) => {
    expect(filesWith(pattern)).toEqual([]);
  });

  it("write no radius or shadow of their own", () => {
    const literal = /(?:border-radius|box-shadow):\s*(?!\s|var\(|0;|none;)/u;

    expect(filesWith(literal)).toEqual([]);
  });
});
