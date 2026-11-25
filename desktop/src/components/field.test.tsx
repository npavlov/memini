import { describe, expect, it } from "vitest";

import { Field, FieldGroup } from "./field";
import { Checkbox, TextInput } from "./inputs";
import { markupOf } from "./markup";

const noop = (): void => undefined;

describe("Field", () => {
  it("wraps its control in a label, so the label's words reach the control", () => {
    const markup = markupOf(
      <Field label="Name">
        <TextInput value="" onChange={noop} />
      </Field>,
    );

    expect(markup).toMatch(/^<label class="field"><span class="field-label">Name<\/span><input /u);
  });

  it("shows a note beside the label and a hint under the control", () => {
    const markup = markupOf(
      <Field label="limit" note="1 to 100" hint="The most results.">
        <TextInput value="" onChange={noop} />
      </Field>,
    );

    expect(markup).toContain('<span class="field-note">1 to 100</span>');
    expect(markup).toMatch(/<input [^>]*\/><small class="field-hint">The most results\.<\/small>/u);
  });

  it("leaves out an empty note and an empty hint", () => {
    const markup = markupOf(
      <Field label="Name" note="" hint="">
        <TextInput value="" onChange={noop} />
      </Field>,
    );

    expect(markup).not.toContain("field-note");
    expect(markup).not.toContain("field-hint");
  });

  it("takes the free width of its row when asked to grow", () => {
    const markup = markupOf(
      <Field label="Path" grow>
        <TextInput value="" onChange={noop} />
      </Field>,
    );

    expect(markup).toContain('class="field field-grow"');
  });
});

describe("FieldGroup", () => {
  it("groups its controls under a legend", () => {
    const markup = markupOf(
      <FieldGroup label="What it may do" inline>
        <Checkbox checked={false} onChange={noop}>
          read
        </Checkbox>
      </FieldGroup>,
    );

    expect(markup).toMatch(/^<fieldset class="field field-group"><legend><span class="field-label">What it may do<\/span><\/legend>/u);
    expect(markup).toContain('class="field-options field-options-inline"');
  });
});
