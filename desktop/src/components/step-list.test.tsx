import { describe, expect, it } from "vitest";

import { markupOf } from "./markup";
import { StepList } from "./step-list";
import type { Step } from "./step-list";

function step(changes: Partial<Step>): Step {
  return { id: "docker", title: "Check Docker", state: "waiting", ...changes };
}

describe("StepList", () => {
  it("is an ordered list with the state of each step in words", () => {
    const markup = markupOf(<StepList steps={[step({})]} />);

    expect(markup).toBe(
      '<ol class="steps"><li class="step"><span class="step-title">Check Docker <span class="pill pill-idle">waiting</span></span></li></ol>',
    );
  });

  it.each([
    ["running", "pill-info"],
    ["done", "pill-good"],
    ["failed", "pill-bad"],
  ] as const)("colours a step that is %s", (state, tone) => {
    expect(markupOf(<StepList steps={[step({ state })]} />)).toContain(`class="pill ${tone}">${state}<`);
  });

  it("marks the step that runs as the current one", () => {
    expect(markupOf(<StepList steps={[step({ state: "running" })]} />)).toContain('aria-current="step"');
  });

  it("shows why a step failed as a failure, and what a running step does as muted text", () => {
    const failed = markupOf(<StepList steps={[step({ state: "failed", detail: "Docker is not running" })]} />);
    const running = markupOf(<StepList steps={[step({ state: "running", detail: "Asking Docker…" })]} />);

    expect(failed).toContain('<p class="text text-failure text-small">Docker is not running</p>');
    expect(running).toContain('<p class="text text-muted text-small">Asking Docker…</p>');
  });

  it("shows markup in a detail as text", () => {
    const markup = markupOf(<StepList steps={[step({ state: "failed", detail: "<b>x</b>" })]} />);

    expect(markup).toContain("&lt;b&gt;x&lt;/b&gt;");
  });
});
