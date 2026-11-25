/// <reference types="node" />
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { Toggle } from "./toggle";

const styles = readFileSync(fileURLToPath(new URL("components.css", import.meta.url)), "utf8");

const change = (): undefined => undefined;

/** The markup with the generated ids replaced by `ID`, so a test can say all of it. */
function markup(element: Parameters<typeof markupOf>[0]): string {
  const html = markupOf(element);
  const id = /<button[^>]* id="([^"]+)"/u.exec(html)?.[1] ?? "no id";
  return html.replaceAll(id, "ID");
}

describe("Toggle", () => {
  it("is a button with the role of a switch, off, named by the label beside it", () => {
    const html = markup(<Toggle label="MCP endpoint" on={false} onChange={change} />);

    expect(html).toBe(
      '<span class="toggle"><button type="button" role="switch" id="ID" class="toggle-track" aria-checked="false"><span class="toggle-knob"></span></button><label class="toggle-label" for="ID">MCP endpoint</label></span>',
    );
  });

  it("says that it is on", () => {
    const html = markup(<Toggle label="MCP endpoint" on onChange={change} />);

    expect(html).toContain('class="toggle-track toggle-on" aria-checked="true"');
  });

  it("stays reachable while the change runs, and says what runs", () => {
    const html = markup(<Toggle label="MCP endpoint" on onChange={change} busy="The server restarts." />);

    expect(html).toContain(
      'class="toggle-track toggle-on toggle-busy" aria-checked="true" aria-busy="true" aria-disabled="true" aria-describedby="ID-note"',
    );
    expect(html).toContain('<span class="toggle-note" id="ID-note">The server restarts.</span>');
  });

  it("says why it cannot be worked, and is not marked as busy", () => {
    const html = markup(
      <Toggle label="MCP endpoint" on={false} onChange={change} unavailable="Its operator sets it." />,
    );

    expect(html).toContain(
      'class="toggle-track toggle-unavailable" aria-checked="false" aria-disabled="true" aria-describedby="ID-note"',
    );
    expect(html).toContain('<span class="toggle-note" id="ID-note">Its operator sets it.</span>');
  });

  it.each([null, ""])("has no note and can be worked when the reason is %j", (reason) => {
    const html = markup(<Toggle label="MCP endpoint" on onChange={change} busy={reason} unavailable={reason} />);

    expect(html).not.toMatch(/aria-disabled|aria-busy|toggle-note/u);
  });

  it("is never disabled for the keyboard: the reason must stay reachable", () => {
    const html = markup(<Toggle label="MCP endpoint" on onChange={change} busy="Working." />);

    expect(html).not.toMatch(/\sdisabled\b/u);
  });

  it("shows markup in a label and in a reason as text", () => {
    const html = markup(<Toggle label="<b>x</b>" on onChange={change} unavailable="<i>y</i>" />);

    expect(html).toContain("&lt;b&gt;x&lt;/b&gt;");
    expect(html).toContain("&lt;i&gt;y&lt;/i&gt;");
  });
});

describe("the styles of the Toggle", () => {
  it("fits the height of a control", () => {
    expect(styles).toMatch(/\.toggle \{[^}]*min-height: var\(--control-height\);/u);
  });

  it.each([
    ["the track when off", /\.toggle-track \{[^}]*background: var\(--control-line\);/u],
    ["the track when on", /\.toggle-on \{[^}]*background: var\(--accent\);/u],
    ["the knob", /\.toggle-knob \{[^}]*background: var\(--toggle-knob\);/u],
  ])("takes the colour of %s from the tokens", (_what, rule) => {
    expect(styles).toMatch(rule);
  });

  it("does not move or spin for a reader who asked for less motion", () => {
    expect(styles).toMatch(
      /@media \(prefers-reduced-motion: reduce\) \{\s*\.toggle-track,\s*\.toggle-knob,\s*\.toggle-knob::after \{\s*transition: none;\s*animation: none;/u,
    );
  });
});
