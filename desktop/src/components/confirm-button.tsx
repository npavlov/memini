import { useState } from "react";
import type { ReactElement } from "react";

import { Button } from "./button";

export interface ConfirmButtonProps {
  readonly label: string;
  /** What the second click will do, in the user's words. */
  readonly question: string;
  readonly confirmLabel: string;
  /** What runs, such as "Deleting…": shown on the button from the second click until it settles. */
  readonly working: string;
  /** Another action runs that this one must wait for. */
  readonly disabled?: boolean;
  readonly onConfirm: () => Promise<void>;
}

/**
 * A button for something that cannot be taken back: the first click asks, the second acts. From
 * the second click until the action settles the button says what runs and cannot be clicked.
 */
export function ConfirmButton(props: ConfirmButtonProps): ReactElement {
  const [asking, setAsking] = useState(false);
  const [running, setRunning] = useState(false);
  if (!asking) {
    return (
      <Button
        disabled={props.disabled === true}
        busy={running ? props.working : null}
        onClick={() => {
          setAsking(true);
        }}
      >
        {props.label}
      </Button>
    );
  }
  const confirm = async (): Promise<void> => {
    setAsking(false);
    setRunning(true);
    try {
      await props.onConfirm();
    } finally {
      setRunning(false);
    }
  };
  return (
    <span className="confirm">
      <span>{props.question}</span>
      <Button variant="danger" onClick={confirm}>
        {props.confirmLabel}
      </Button>
      <Button
        onClick={() => {
          setAsking(false);
        }}
      >
        Cancel
      </Button>
    </span>
  );
}
