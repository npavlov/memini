import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { Tree, TreeItem } from "./tree";

describe("Tree", () => {
  it("is a list that says what it is a tree of", () => {
    const markup = markupOf(
      <Tree label="Domains of port">
        <TreeItem kind="Domain" title="berths" />
      </Tree>,
    );

    expect(markup).toBe(
      '<ul class="tree" aria-label="Domains of port"><li class="tree-item"><div class="tree-line"><span class="tree-kind">Domain</span><span class="tree-title">berths</span></div></li></ul>',
    );
  });

  it("puts the items inside an item into a list of their own, one level in", () => {
    const markup = markupOf(
      <TreeItem kind="Domain" title="berths">
        <TreeItem kind="Subdomain" title="holds" />
      </TreeItem>,
    );

    expect(markup).toContain(
      '<ul class="tree tree-inside"><li class="tree-item"><div class="tree-line"><span class="tree-kind">Subdomain</span><span class="tree-title">holds</span></div></li></ul>',
    );
  });

  it("shows a count at the end of the line and a description under it", () => {
    const markup = markupOf(<TreeItem kind="Domain" title="berths" aside="12 files" detail="Where ships lie." />);

    expect(markup).toContain(
      '<span class="tree-aside">12 files</span></div><div class="tree-detail">Where ships lie.</div>',
    );
  });

  it("shows markup in a title as text", () => {
    const markup = markupOf(<TreeItem kind="Domain" title="<b>berths</b>" />);

    expect(markup).toContain("&lt;b&gt;berths&lt;/b&gt;");
  });
});
