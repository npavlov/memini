import type { ReactElement, ReactNode } from "react";

import { Icon } from "./icons";
import type { IconName } from "./icons";

/** Secondary is the default; primary is the one main action of a form; danger cannot be undone. */
export type ButtonVariant = "secondary" | "primary" | "danger";

export interface ButtonProps {
  readonly children: ReactNode;
  readonly variant?: ButtonVariant;
  /** Submits the form the button stands in. */
  readonly submit?: boolean;
  readonly disabled?: boolean;
  /**
   * What runs, in words such as "Syncing…", while the action the button started runs: the
   * button shows them with a turning ring and cannot be clicked. Null or left out: idle.
   */
  readonly busy?: string | null;
  readonly onClick?: () => unknown;
  /** For a button that opens and closes a region: whether it is open, and the region's id. */
  readonly expanded?: boolean;
  readonly controls?: string;
}

function working(busy: string | null | undefined): string | null {
  return busy === undefined || busy === null || busy === "" ? null : busy;
}

/**
 * The app's button. Every button has the same height, padding and font, whatever its variant:
 * a variant changes colours only, so buttons in one row always line up.
 */
export function Button(props: ButtonProps): ReactElement {
  const { children, variant = "secondary", submit = false, disabled = false, onClick } = props;
  const busy = working(props.busy);
  return (
    <button
      type={submit ? "submit" : "button"}
      className={`button button-${variant}${busy === null ? "" : " button-busy"}`}
      disabled={disabled || busy !== null}
      aria-busy={busy === null ? undefined : true}
      aria-expanded={props.expanded}
      aria-controls={props.controls}
      onClick={onClick}
    >
      {busy === null ? (
        children
      ) : (
        <>
          <span className="button-spinner" aria-hidden="true" />
          {busy}
        </>
      )}
    </button>
  );
}

export interface IconButtonProps {
  /** What the button does, for a screen reader and as a tooltip: the icon alone does not say. */
  readonly label: string;
  readonly icon: IconName;
  readonly disabled?: boolean;
  /** What runs, as for a Button: it becomes the label, and a turning ring takes the icon's place. */
  readonly busy?: string | null;
  readonly onClick: () => unknown;
}

/** A square button that shows an icon. As high as a Button, so the two stand in one row. */
export function IconButton(props: IconButtonProps): ReactElement {
  const { label, icon, disabled = false, onClick } = props;
  const busy = working(props.busy);
  return (
    <button
      type="button"
      className={`button button-secondary button-icon${busy === null ? "" : " button-busy"}`}
      aria-label={busy ?? label}
      title={busy ?? label}
      disabled={disabled || busy !== null}
      aria-busy={busy === null ? undefined : true}
      onClick={onClick}
    >
      {busy === null ? <Icon name={icon} /> : <span className="button-spinner" aria-hidden="true" />}
    </button>
  );
}
