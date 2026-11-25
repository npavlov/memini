import type { ReactElement, ReactNode } from "react";

import type { Tone } from "./tone";

export interface PillProps {
  readonly tone: Tone;
  readonly children: ReactNode;
}

/** A short state, coloured by its tone. The words carry the meaning; the colour repeats it. */
export function Pill({ tone, children }: PillProps): ReactElement {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}
