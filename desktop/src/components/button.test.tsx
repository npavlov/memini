/// <reference types="node" />
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { Button, IconButton } from "./button";
import { markupOf } from "./markup";

const styles = readFileSync(fileURLToPath(new URL("./components.css", import.meta.url)), "utf8");

/** The properties a rule sets, for a selector written exactly as in the style sheet. */
function propertiesOf(selector: string): readonly string[] {
  const start = styles.indexOf(`\n${selector} {`);
  const body = start < 0 ? "" : styles.slice(styles.indexOf("{", start) + 1, styles.indexOf("}", start));
  return Array.from(body.matchAll(/^\s*([a-z-]+):/gmu), (match) => match[1] ?? "");
}

const SIZE = ["height", "min-height", "padding", "font", "font-size", "font-weight", "line-height"];
const noop = (): void => undefined;

describe("Button", () => {
  it("renders a secondary button that does not submit, by default", () => {
    expect(markupOf(<Button>Refresh</Button>)).toBe(
      '<button type="button" class="button button-secondary">Refresh</button>',
    );
  });

  it.each(["primary", "danger", "secondary"] as const)("takes its colours from the %s variant", (variant) => {
    expect(markupOf(<Button variant={variant}>Go</Button>)).toContain(`class="button button-${variant}"`);
  });

  it("submits its form when asked to", () => {
    expect(markupOf(<Button submit>Save</Button>)).toContain('type="submit"');
  });

  it("is disabled when told so", () => {
    expect(markupOf(<Button disabled>Save</Button>)).toContain("disabled");
  });

  it("says whether the region it opens is open", () => {
    const markup = markupOf(
      <Button expanded={false} controls="details">
        Details
      </Button>,
    );

    expect(markup).toContain('aria-expanded="false"');
    expect(markup).toContain('aria-controls="details"');
  });

  it("says what runs, with a turning ring, and cannot be clicked while busy", () => {
    expect(markupOf(<Button busy="Syncing…">Sync now</Button>)).toBe(
      '<button type="button" class="button button-secondary button-busy" disabled="" aria-busy="true">' +
        '<span class="button-spinner" aria-hidden="true"></span>Syncing…</button>',
    );
  });

  it.each([[null], [""]])("is idle when busy is %j", (busy) => {
    expect(markupOf(<Button busy={busy}>Sync now</Button>)).toBe(
      '<button type="button" class="button button-secondary">Sync now</button>',
    );
  });

  it("shows text it is given as text", () => {
    expect(markupOf(<Button>{"<b>Save</b>"}</Button>)).toContain("&lt;b&gt;Save&lt;/b&gt;");
  });
});

describe("IconButton", () => {
  it("names itself for a screen reader, because an icon does not", () => {
    const markup = markupOf(<IconButton label="Check now" icon="refresh" onClick={noop} />);

    expect(markup).toContain('aria-label="Check now"');
    expect(markup).toContain('class="button button-secondary button-icon"');
  });

  it("names what runs and turns a ring in place of its icon while busy", () => {
    const markup = markupOf(<IconButton label="Check now" icon="refresh" busy="Checking…" onClick={noop} />);

    expect(markup).toContain('aria-label="Checking…"');
    expect(markup).toContain('disabled=""');
    expect(markup).toContain('<span class="button-spinner" aria-hidden="true"></span>');
    expect(markup).not.toContain("<svg");
  });
});

describe("the button styles", () => {
  it("set the height of buttons and inputs in one rule", () => {
    expect(styles).toContain(".button,\n.input {\n  height: var(--control-height);");
  });

  it.each([
    [".button-primary"],
    [".button-danger"],
    [".button-primary:hover:not(:disabled)"],
    [".button-primary:active:not(:disabled)"],
    [".button-secondary:hover:not(:disabled)"],
    [".button:disabled"],
  ])("change colours only in %s, never the size", (selector) => {
    const sizes = propertiesOf(selector).filter((property) => SIZE.includes(property));

    expect(propertiesOf(selector).length).toBeGreaterThan(0);
    expect(sizes).toEqual([]);
  });

  it("give the primary button a solid accent fill, no border and one small shadow", () => {
    const start = styles.indexOf("\n.button-primary {");
    const rule = styles.slice(start, styles.indexOf("}", start));

    expect(rule).toContain("border: none;");
    expect(rule).toContain("background: var(--accent);");
    expect(rule).toContain("box-shadow: var(--shadow-primary);");
  });

  it("keep the primary button clickable to the eye: darker under the pointer and when pressed", () => {
    expect(styles).toContain(".button-primary:hover:not(:disabled) {\n  background: var(--accent-hover);");
    expect(styles).toContain(".button-primary:active:not(:disabled) {\n  background: var(--accent-pressed);");
  });

  it("give the secondary button a border of one pixel", () => {
    expect(styles).toContain(".button-secondary,\n.button-danger {\n  border: 1px solid var(--control-line);");
  });

  it("draw the focus ring for the keyboard only", () => {
    expect(styles).toContain(":focus {\n  outline: none;\n}");
    expect(styles).toContain(":focus-visible {\n  outline: 2px solid var(--focus-ring);");
  });

  it("keep a busy button as large as an idle one: the ring has the size of a line of text", () => {
    expect(propertiesOf(".button-busy:disabled")).toContain("cursor");
    expect(propertiesOf(".button-busy:disabled").filter((property) => SIZE.includes(property))).toEqual([]);
    expect(styles).toContain(".button-spinner {\n  width: 1em;\n  height: 1em;");
  });

  it("stop the ring turning for a reader who asks for less motion", () => {
    expect(styles).toMatch(/prefers-reduced-motion: reduce\)[^}]*\.button-spinner/u);
  });

  it("have no size variant", () => {
    expect(styles).not.toMatch(/\.button-(?:small|large|compact)/u);
  });
});
