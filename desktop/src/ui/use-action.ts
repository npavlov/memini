import { useCallback, useState } from "react";

import { toAppError } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";

export interface Action<A> {
  readonly pending: boolean;
  readonly error: AppError | null;
  /** Runs the action. Never rejects: a failure becomes `error`. */
  readonly run: (argument: A) => Promise<void>;
  readonly clearError: () => void;
}

/** Something the user starts with a click: its progress, and its failure as an AppError. */
export function useAction<A = void>(act: (argument: A) => Promise<void>): Action<A> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<AppError | null>(null);
  const run = useCallback(
    async (argument: A): Promise<void> => {
      setPending(true);
      setError(null);
      try {
        await act(argument);
      } catch (failure) {
        setError(toAppError(failure));
      } finally {
        setPending(false);
      }
    },
    [act],
  );
  const clearError = useCallback((): void => {
    setError(null);
  }, []);
  return { pending, error, run, clearError };
}
