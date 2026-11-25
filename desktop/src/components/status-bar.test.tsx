import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { StatusBarActions, StatusBarFrame } from "./status-bar";

describe("StatusBarFrame", () => {
  it("is the page's footer and shows its line", () => {
    expect(markupOf(<StatusBarFrame>line</StatusBarFrame>)).toBe('<footer class="statusbar">line</footer>');
  });

  it("opens its details as a named region above the line", () => {
    const markup = markupOf(
      <StatusBarFrame details={<p>details</p>} detailsId="status-details">
        line
      </StatusBarFrame>,
    );

    expect(markup).toBe(
      '<footer class="statusbar"><div class="statusbar-details" id="status-details" role="region" aria-label="Status details"><p>details</p></div>line</footer>',
    );
  });

  it("has no details region while it is closed", () => {
    const markup = markupOf(<StatusBarFrame details={null}>line</StatusBarFrame>);

    expect(markup).not.toContain("statusbar-details");
  });
});

describe("StatusBarActions", () => {
  it("groups the bar's buttons", () => {
    expect(markupOf(<StatusBarActions>x</StatusBarActions>)).toBe('<span class="statusbar-actions">x</span>');
  });
});
