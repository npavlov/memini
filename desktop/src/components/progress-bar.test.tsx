import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { ProgressBar } from "./progress-bar";

describe("ProgressBar", () => {
  it("fills as far as the work is and says it to a screen reader", () => {
    const markup = markupOf(<ProgressBar label="Files of harbour" value={312} max={1472} detail="312 of 1,472 files" />);

    expect(markup).toContain('role="progressbar"');
    expect(markup).toContain('aria-label="Files of harbour"');
    expect(markup).toContain('aria-valuenow="312"');
    expect(markup).toContain('aria-valuemax="1472"');
    expect(markup).toContain("width:21%");
    expect(markup).toContain('<p class="progress-detail">312 of 1,472 files</p>');
  });

  it("keeps the value between none and all", () => {
    expect(markupOf(<ProgressBar label="x" value={20} max={10} />)).toContain("width:100%");
    expect(markupOf(<ProgressBar label="x" value={-3} max={10} />)).toContain("width:0%");
  });

  it("moves without a value while the total is not known", () => {
    const markup = markupOf(<ProgressBar label="Files" value={0} max={0} />);

    expect(markup).toContain("progress-unknown");
    expect(markup).not.toContain("aria-valuenow");
  });
});
