import { useState } from "react";
import type { ReactElement } from "react";

export interface ConfirmButtonProps {
  readonly label: string;
  /** What the second click will do, in the user's words. */
  readonly question: string;
  readonly confirmLabel: string;
  readonly disabled?: boolean;
  readonly onConfirm: () => Promise<void>;
}

/** A button for something that cannot be taken back: the first click asks, the second acts. */
export function ConfirmButton(props: ConfirmButtonProps): ReactElement {
  const [asking, setAsking] = useState(false);
  if (!asking) {
    return (
      <button
        type="button"
        className="button small"
        disabled={props.disabled === true}
        onClick={() => {
          setAsking(true);
        }}
      >
        {props.label}
      </button>
    );
  }
  const confirm = async (): Promise<void> => {
    setAsking(false);
    await props.onConfirm();
  };
  return (
    <span className="confirm">
      <span>{props.question}</span>
      <button type="button" className="button small danger" onClick={confirm}>
        {props.confirmLabel}
      </button>
      <button
        type="button"
        className="button small"
        onClick={() => {
          setAsking(false);
        }}
      >
        Cancel
      </button>
    </span>
  );
}
