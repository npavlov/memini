import type { ReactElement } from "react";

import { Text } from "./layout";
import { Pill } from "./pill";
import type { Tone } from "./tone";

/** Where a step of a process stands. */
export type StepState = "waiting" | "running" | "done" | "failed";

export interface Step {
  readonly id: string;
  readonly title: string;
  readonly state: StepState;
  /** A sentence under the title: what the step does while it runs, or why it failed. */
  readonly detail?: string | null;
}

export interface StepListProps {
  readonly steps: readonly Step[];
}

const SHOWN: Readonly<Record<StepState, { readonly tone: Tone; readonly label: string }>> = {
  waiting: { tone: "idle", label: "waiting" },
  running: { tone: "info", label: "running" },
  done: { tone: "good", label: "done" },
  failed: { tone: "bad", label: "failed" },
};

/** The steps of a process in their order, each with its state: what is done, what runs, what failed. */
export function StepList({ steps }: StepListProps): ReactElement {
  return (
    <ol className="steps">
      {steps.map((step) => (
        <li key={step.id} className="step" aria-current={step.state === "running" ? "step" : undefined}>
          <span className="step-title">
            {step.title} <Pill tone={SHOWN[step.state].tone}>{SHOWN[step.state].label}</Pill>
          </span>
          {step.detail === undefined || step.detail === null || step.detail === "" ? null : (
            <Text tone={step.state === "failed" ? "failure" : "muted"} small>
              {step.detail}
            </Text>
          )}
        </li>
      ))}
    </ol>
  );
}
