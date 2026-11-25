/// <reference types="node" />
// The fonts are bundled with the app and their licence travels with them: the SIL Open Font
// License asks that its notice goes wherever the font files go.

import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("../..", import.meta.url));
const read = (path: string): string => readFileSync(`${DESKTOP}/${path}`, "utf8");

const fonts = read("src/components/fonts.css");
const files = Array.from(fonts.matchAll(/url\("([^"]+)"\)/gu), (match) => match[1] ?? "");

describe("the bundled fonts", () => {
  it("are the fifteen faces of IBM Plex Sans and IBM Plex Mono the app uses", () => {
    expect(files).toHaveLength(15);
    expect(files.every((file) => /^@fontsource\/ibm-plex-(?:sans|mono)\/files\/[\w-]+\.woff2$/u.test(file))).toBe(true);
  });

  it.each(files)("exist in the pinned package: %s", (file) => {
    expect(existsSync(`${DESKTOP}/node_modules/${file}`)).toBe(true);
  });

  it("are loaded from the app itself, never from another origin", () => {
    expect(fonts).not.toMatch(/https?:/u);
  });

  it.each([
    ["Sans", "@fontsource/ibm-plex-sans"],
    ["Mono", "@fontsource/ibm-plex-mono"],
  ])("ship the licence notice of IBM Plex %s as its package gives it", (family, name) => {
    const shipped = read(`public/licenses/IBM-Plex-${family}-OFL.txt`);

    expect(shipped).toBe(read(`node_modules/${name}/LICENSE`));
    expect(shipped).toContain("SIL OPEN FONT LICENSE Version 1.1");
    expect(shipped).toContain("IBM Corp.");
  });

  it.each(["csp", "devCsp"])("are allowed by the content security policy %s, from the app only", (policy) => {
    const config: unknown = JSON.parse(read("src-tauri/tauri.conf.json"));

    expect(config).toMatchObject({ app: { security: { [policy]: { "font-src": "'self'" } } } });
  });
});
