import { useId } from "react";
import type { ReactElement } from "react";

export interface ToggleProps {
  /** What the switch switches, in words beside it. Clicking them works the switch. */
  readonly label: string;
  readonly on: boolean;
  /** Called with the state the user asks for. Not called while busy or unavailable. */
  readonly onChange: (on: boolean) => unknown;
  /** What is running while the change is made. The switch waits and cannot be worked. */
  readonly busy?: string | null;
  /** Why the switch cannot be worked now. */
  readonly unavailable?: string | null;
}

function filled(text: string | null | undefined): string | null {
  return text === undefined || text === null || text === "" ? null : text;
}

/**
 * A switch for one thing that is on or off and changes at once. It stays in the order of the
 * keyboard while it is busy or unavailable, so the reason beside it can be reached and is read
 * out with it; a click then does nothing.
 */
export function Toggle({ label, on, onChange, busy, unavailable }: ToggleProps): ReactElement {
  const id = useId();
  const working = filled(busy);
  const note = working ?? filled(unavailable);
  const state = working === null ? (note === null ? "" : " toggle-unavailable") : " toggle-busy";
  return (
    <span className="toggle">
      <button
        type="button"
        role="switch"
        id={id}
        className={`toggle-track${on ? " toggle-on" : ""}${state}`}
        aria-checked={on}
        aria-busy={working === null ? undefined : true}
        aria-disabled={note === null ? undefined : true}
        aria-describedby={note === null ? undefined : `${id}-note`}
        onClick={() => {
          if (note === null) {
            onChange(!on);
          }
        }}
      >
        <span className="toggle-knob" />
      </button>
      <label className="toggle-label" htmlFor={id}>
        {label}
      </label>
      {note === null ? null : (
        <span className="toggle-note" id={`${id}-note`}>
          {note}
        </span>
      )}
    </span>
  );
}
