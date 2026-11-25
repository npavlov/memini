import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { Actions, Card, EmptyState, Mono, Panel, Screen, Section } from "./surfaces";

describe("Screen", () => {
  it("names the screen in the one first-level heading and says what it is for", () => {
    const markup = markupOf(
      <Screen title="Tokens" lead="Who may read.">
        <p>body</p>
      </Screen>,
    );

    expect(markup).toContain("<h1>Tokens</h1>");
    expect(markup).toContain('<p class="text text-muted">Who may read.</p>');
    expect(markup).toContain("<p>body</p>");
  });
});

describe("Panel", () => {
  it("titles its content with a second-level heading", () => {
    expect(markupOf(<Panel title="Endpoint">x</Panel>)).toContain("<h2>Endpoint</h2>");
  });

  it("puts its actions beside the title", () => {
    const markup = markupOf(
      <Panel title="Endpoint" actions={<span>act</span>}>
        x
      </Panel>,
    );

    expect(markup).toContain('<div class="actions"><span>act</span></div>');
  });

  it("has no row of actions when it is given none", () => {
    expect(markupOf(<Panel title="Endpoint">x</Panel>)).not.toContain('class="actions"');
  });
});

describe("Section", () => {
  it("titles a part of a panel with a third-level heading, with what stands beside it", () => {
    const markup = markupOf(
      <Section title="harbour" aside={<span>150 records</span>}>
        x
      </Section>,
    );

    expect(markup).toContain("<h3>harbour</h3><span>150 records</span>");
  });
});

describe("Card", () => {
  it("shows a title, a line under it and its content", () => {
    const markup = markupOf(
      <Card title="src/lamp.py" subtitle="code_file · harbour">
        <p>body</p>
      </Card>,
    );

    expect(markup).toBe(
      '<article class="card"><p class="card-title">src/lamp.py</p><p class="text text-muted text-small">code_file · harbour</p><p>body</p></article>',
    );
  });

  it("leaves out an empty title and an empty subtitle", () => {
    expect(markupOf(<Card title="" subtitle="" />)).toBe('<article class="card"></article>');
  });

  it("shows markup in a title as text", () => {
    expect(markupOf(<Card title="<b>x</b>" />)).toContain("&lt;b&gt;x&lt;/b&gt;");
  });
});

describe("EmptyState", () => {
  it("says what is missing and is announced as a status", () => {
    const markup = markupOf(
      <EmptyState title="No connection yet">
        <p>Add one.</p>
      </EmptyState>,
    );

    expect(markup).toBe(
      '<div class="empty-state" role="status"><p class="empty-state-title">No connection yet</p><p>Add one.</p></div>',
    );
  });
});

describe("Actions and Mono", () => {
  it("lay buttons out in a row", () => {
    expect(markupOf(<Actions>x</Actions>)).toBe('<div class="actions">x</div>');
  });

  it("set a literal value in the mono font", () => {
    expect(markupOf(<Mono>http://127.0.0.1:6364</Mono>)).toBe('<span class="mono">http://127.0.0.1:6364</span>');
  });
});
