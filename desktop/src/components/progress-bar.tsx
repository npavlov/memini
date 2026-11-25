import type { ReactElement } from "react";

export interface ProgressBarProps {
  /** What moves, for a screen reader: "Files of harbour-api". */
  readonly label: string;
  /** Done so far, at least 0. */
  readonly value: number;
  /** All there is to do; 0 or less shows a bar that only says something runs. */
  readonly max: number;
  /** The words under the bar: "312 of 1,472 files · about 25 minutes left". */
  readonly detail?: string;
}

/**
 * How far a long piece of work is: a bar that fills from the start, with its words under it. A
 * total that is not known yet shows a bar that moves without a value.
 */
export function ProgressBar({ label, value, max, detail }: ProgressBarProps): ReactElement {
  const known = max > 0;
  const done = known ? Math.min(Math.max(value, 0), max) : 0;
  const percent = known ? Math.round((done / max) * 100) : 0;
  return (
    <div className="progress">
      <div
        className={known ? "progress-track" : "progress-track progress-unknown"}
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={known ? max : undefined}
        aria-valuenow={known ? done : undefined}
      >
        <div className="progress-fill" style={{ width: known ? `${String(percent)}%` : undefined }} />
      </div>
      {detail === undefined ? null : <p className="progress-detail">{detail}</p>}
    </div>
  );
}
