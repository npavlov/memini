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
  readonly onClick?: () => unknown;
  /** For a button that opens and closes a region: whether it is open, and the region's id. */
  readonly expanded?: boolean;
  readonly controls?: string;
}

/**
 * The app's button. Every button has the same height, padding and font, whatever its variant:
 * a variant changes colours only, so buttons in one row always line up.
 */
export function Button(props: ButtonProps): ReactElement {
  const { children, variant = "secondary", submit = false, disabled = false, onClick } = props;
  return (
    <button
      type={submit ? "submit" : "button"}
      className={`button button-${variant}`}
      disabled={disabled}
      aria-expanded={props.expanded}
      aria-controls={props.controls}
      onClick={onClick}
    >
      {children}
    </button>
  );
}

export interface IconButtonProps {
  /** What the button does, for a screen reader and as a tooltip: the icon alone does not say. */
  readonly label: string;
  readonly icon: IconName;
  readonly disabled?: boolean;
  readonly onClick: () => unknown;
}

/** A square button that shows an icon. As high as a Button, so the two stand in one row. */
export function IconButton({ label, icon, disabled = false, onClick }: IconButtonProps): ReactElement {
  return (
    <button
      type="button"
      className="button button-secondary button-icon"
      aria-label={label}
      title={label}
      disabled={disabled}
      onClick={onClick}
    >
      <Icon name={icon} />
    </button>
  );
}
