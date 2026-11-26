import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { Brand, NavItem, RowList, SelectableRow, Sidebar, SidebarPicker, Tabs } from "./navigation";

const noop = (): void => undefined;

describe("Sidebar and Brand", () => {
  it("is the navigation of the app, with the brand above its items", () => {
    const markup = markupOf(<Sidebar footer="Version 0.1.0">items</Sidebar>);

    expect(markup).toMatch(/^<nav class="sidebar" aria-label="Screens"><p class="brand">/u);
    expect(markup).toContain('<p class="sidebar-footer">Version 0.1.0</p>');
  });

  it("leaves out an empty footer", () => {
    expect(markupOf(<Sidebar footer="">items</Sidebar>)).not.toContain("sidebar-footer");
  });

  it("puts a header between the brand and the items, and leaves out none", () => {
    const markup = markupOf(<Sidebar header="choice">items</Sidebar>);

    expect(markup).toMatch(/<\/p><div class="sidebar-header">choice<\/div>items/u);
    expect(markupOf(<Sidebar>items</Sidebar>)).not.toContain("sidebar-header");
  });
});

describe("SidebarPicker", () => {
  it("is a labelled choice, the chosen value selected, with no line under it", () => {
    const markup = markupOf(
      <SidebarPicker
        label="Workspace"
        value="harbour"
        options={[
          { value: "harbour", label: "harbour" },
          { value: "quay", label: "quay" },
        ]}
        onChange={noop}
      />,
    );

    expect(markup).toMatch(/^<label class="sidebar-picker"><span class="sidebar-picker-label">Workspace<\/span>/u);
    expect(markup).toContain('<select class="sidebar-picker-select">');
    expect(markup).toContain('<option value="harbour" selected="">harbour</option>');
    expect(markup).not.toContain("<small");
  });

  it("shows the glyph next to the name, which is written in lower case", () => {
    const markup = markupOf(<Brand />);

    expect(markup).toContain('<svg class="glyph"');
    expect(markup).toContain('<span class="brand-name">memini</span>');
  });
});

describe("NavItem", () => {
  it("marks the current screen for the eye and for a screen reader", () => {
    const markup = markupOf(
      <NavItem current onSelect={noop}>
        Data
      </NavItem>,
    );

    expect(markup).toBe(
      '<button type="button" class="nav-item nav-item-current" aria-current="page">Data</button>',
    );
  });

  it("is an ordinary item when it is not the current screen", () => {
    const markup = markupOf(
      <NavItem current={false} onSelect={noop}>
        Data
      </NavItem>,
    );

    expect(markup).toBe('<button type="button" class="nav-item">Data</button>');
  });
});

describe("Tabs", () => {
  const items = [
    { id: "cards", label: "Cards" },
    { id: "raw", label: "Raw JSON" },
  ];

  it("is a list of tabs with a name, of which one is selected", () => {
    const markup = markupOf(<Tabs label="How the answer is shown" items={items} current="raw" onSelect={noop} />);

    expect(markup).toContain('role="tablist" aria-label="How the answer is shown"');
    expect(markup).toContain('<button type="button" role="tab" aria-selected="false" class="tab">Cards</button>');
    expect(markup).toContain(
      '<button type="button" role="tab" aria-selected="true" class="tab tab-current">Raw JSON</button>',
    );
  });
});

describe("SelectableRow", () => {
  it("is a list item whose main part is a button that says whether it is chosen", () => {
    const markup = markupOf(
      <RowList>
        <SelectableRow selected onSelect={noop} title="Harbour team" detail="https://memini.example.org">
          <span>aside</span>
        </SelectableRow>
      </RowList>,
    );

    expect(markup).toMatch(/^<ul class="rows"><li class="row row-selected"><button type="button" class="row-main" aria-pressed="true">/u);
    expect(markup).toContain("<strong>Harbour team</strong>");
    expect(markup).toContain('<span class="text text-muted">https://memini.example.org</span>');
    expect(markup).toContain("</button><span>aside</span></li></ul>");
  });
});
