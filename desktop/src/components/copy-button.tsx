import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { Button } from "./button";

const SHOWN_MS = 2000;

export interface CopyButtonProps {
  readonly text: string;
  readonly label?: string;
  /** Copy as a secret, which clipboard managers are asked not to keep. */
  readonly secret?: boolean;
}

function wordsFor(failed: boolean, copied: boolean, label: string): string {
  if (failed) {
    return "Could not copy";
  }
  return copied ? "Copied" : label;
}

/** Copies a text to the clipboard through the Rust side, and says for a moment that it did. */
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
  return <Button onClick={() => action.run()}>{wordsFor(action.error !== null, copied, label)}</Button>;
}
