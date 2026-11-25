import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { useBackend } from "../ipc/backend-context";
import { useAction } from "./use-action";

const SHOWN_MS = 2000;

export interface CopyButtonProps {
  readonly text: string;
  readonly label?: string;
  /** Copy as a secret, which clipboard managers are asked not to keep. */
  readonly secret?: boolean;
}

export function CopyButton({ text, label = "Copy", secret = false }: CopyButtonProps): ReactElement {
  const backend = useBackend();
  const [copied, setCopied] = useState(false);
  const copy = useCallback(async (): Promise<void> => {
    await (secret ? backend.copySecret(text) : backend.copyText(text));
    setCopied(true);
  }, [backend, secret, text]);
  const action = useAction(copy);
  useEffect(() => {
    if (!copied) {
      return undefined;
    }
    const timer = window.setTimeout(() => {
      setCopied(false);
    }, SHOWN_MS);
    return (): void => {
      window.clearTimeout(timer);
    };
  }, [copied]);
  const words = action.error === null ? (copied ? "Copied" : label) : "Could not copy";
  return (
    <button type="button" className="button small" onClick={() => action.run()}>
      {words}
    </button>
  );
}
