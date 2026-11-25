import { describe, expect, it } from "vitest";

import { AppFrame, Columns, FieldGrid, Form, NoWrap, Splash, Stack, Text } from "./layout";
import { markupOf } from "./markup";

const noop = (): void => undefined;

describe("Text", () => {
  it("is a plain paragraph by default", () => {
    expect(markupOf(<Text>Hello</Text>)).toBe('<p class="text text-plain">Hello</p>');
  });

  it.each(["muted", "hint", "failure"] as const)("takes the %s style", (tone) => {
    expect(markupOf(<Text tone={tone}>x</Text>)).toContain(`class="text text-${tone}"`);
  });

  it("can be small, and can run inside a line", () => {
    const markup = markupOf(
      <Text tone="muted" small inline>
        git
      </Text>,
    );

    expect(markup).toBe('<span class="text text-muted text-small">git</span>');
  });

  it("shows markup as text", () => {
    expect(markupOf(<Text>{"<i>x</i>"}</Text>)).toContain("&lt;i&gt;x&lt;/i&gt;");
  });
});

describe("Form", () => {
  it("is a form that goes nowhere by itself", () => {
    expect(markupOf(<Form onSubmit={noop}>x</Form>)).toBe('<form class="form">x</form>');
  });

  it("lays a field and its button out in one row when inline", () => {
    const markup = markupOf(
      <Form inline onSubmit={noop}>
        x
      </Form>,
    );

    expect(markup).toContain('class="form form-inline"');
  });
});

describe("the layout components", () => {
  it.each([
    ["Stack", <Stack key="s">x</Stack>, '<div class="stack">x</div>'],
    ["Columns", <Columns key="c">x</Columns>, '<div class="columns">x</div>'],
    ["FieldGrid", <FieldGrid key="g">x</FieldGrid>, '<div class="field-grid">x</div>'],
    ["NoWrap", <NoWrap key="n">30 min</NoWrap>, '<span class="nowrap">30 min</span>'],
    ["Splash", <Splash key="p">x</Splash>, '<div class="splash">x</div>'],
  ])("%s wraps its content in its one element", (_name, element, markup) => {
    expect(markupOf(element)).toBe(markup);
  });
});

describe("AppFrame", () => {
  it("puts the screen into the page's main region, between the sidebar and the status bar", () => {
    const markup = markupOf(
      <AppFrame sidebar={<nav>nav</nav>} statusBar={<footer>bar</footer>}>
        screen
      </AppFrame>,
    );

    expect(markup).toBe('<div class="app-frame"><nav>nav</nav><main>screen</main><footer>bar</footer></div>');
  });
});
