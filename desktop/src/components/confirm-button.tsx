import { useState } from "react";
import type { ReactElement } from "react";

import { Button } from "./button";

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
      <Button
        disabled={props.disabled === true}
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
    await props.onConfirm();
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
