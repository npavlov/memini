import { describe, expect, it } from "vitest";

import { Checkbox, Select, TextArea, TextInput } from "./inputs";
import { markupOf } from "./markup";

const noop = (): void => undefined;

describe("TextInput", () => {
  it("is a text field with the value it is given", () => {
    const markup = markupOf(<TextInput value="Harbour team" placeholder="Name" maxLength={80} onChange={noop} />);

    expect(markup).toContain('type="text"');
    expect(markup).toContain('class="input"');
    expect(markup).toContain('value="Harbour team"');
    expect(markup).toContain('placeholder="Name"');
    expect(markup).toContain('maxLength="80"');
  });

  it("sets code in the mono font and keeps the browser from correcting it", () => {
    const markup = markupOf(<TextInput kind="code" value="https://memini.example.org" onChange={noop} />);

    expect(markup).toContain('class="input input-code"');
    expect(markup).toContain('spellCheck="false"');
    expect(markup).toContain('autoCapitalize="off"');
  });

  it("asks for digits when it holds a number", () => {
    expect(markupOf(<TextInput kind="number" value="10" onChange={noop} />)).toContain('inputMode="numeric"');
  });

  it("keeps markup in a value as text", () => {
    const markup = markupOf(<TextInput value={'"><script>x</script>'} onChange={noop} />);

    expect(markup).not.toContain("<script>");
  });
});

describe("TextArea", () => {
  it("holds several lines of literal text", () => {
    const markup = markupOf(<TextArea value={"chat\nmail"} rows={3} onChange={noop} />);

    expect(markup).toContain('class="input input-code input-lines"');
    expect(markup).toContain('rows="3"');
    expect(markup).toContain("chat\nmail");
  });
});

describe("Select", () => {
  const options = [
    { value: "", label: "Every workspace" },
    { value: "harbour", label: "harbour" },
  ];

  it("offers each option with its label and marks the chosen one", () => {
    const markup = markupOf(<Select value="harbour" options={options} onChange={noop} />);

    expect(markup).toContain('<option value="">Every workspace</option>');
    expect(markup).toContain('<option value="harbour" selected="">harbour</option>');
  });
});

describe("Checkbox", () => {
  it("is a box with its words in one label, so the words can be clicked", () => {
    const markup = markupOf(
      <Checkbox checked onChange={noop}>
        Allow plain HTTP
      </Checkbox>,
    );

    expect(markup).toMatch(/^<label class="check"><input type="checkbox" checked=""\/><span>Allow plain HTTP<\/span><\/label>$/u);
  });
});
