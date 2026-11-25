import { describe, expect, it } from "vitest";

import { CodeBlock, Fact, Facts } from "./code-block";
import { markupOf } from "./markup";

describe("CodeBlock", () => {
  it("shows literal text exactly as it is", () => {
    expect(markupOf(<CodeBlock text={"brew install x\n  --flag"} />)).toBe(
      '<pre class="code"><code>brew install x\n  --flag</code></pre>',
    );
  });

  it("has a caption and a copy button when it has a label", () => {
    const markup = markupOf(<CodeBlock label="Run once" text="codex mcp add memini" />);

    expect(markup).toMatch(/^<figure class="code-block"><figcaption><span>Run once<\/span><button /u);
    expect(markup).toContain(">Copy</button>");
  });

  it.each([
    ["wraps long lines", <CodeBlock key="w" text="x" wrap />, "code code-wrap"],
    ["caps its height", <CodeBlock key="b" text="x" bounded />, "code code-bounded"],
    ["marks and wraps a secret", <CodeBlock key="s" text="x" secret />, "code code-wrap code-secret"],
  ])("%s when asked to", (_what, element, classes) => {
    expect(markupOf(element)).toContain(`<pre class="${classes}">`);
  });

  it("shows markup in its text as text", () => {
    expect(markupOf(<CodeBlock text="<script>x</script>" />)).toContain("&lt;script&gt;x&lt;/script&gt;");
  });
});

describe("Facts and Fact", () => {
  it("list named values as a description list", () => {
    const markup = markupOf(
      <Facts>
        <Fact name="Address">http://127.0.0.1:6364</Fact>
      </Facts>,
    );

    expect(markup).toBe(
      '<dl class="facts"><div class="fact"><dt>Address</dt><dd>http://127.0.0.1:6364</dd></div></dl>',
    );
  });
});
