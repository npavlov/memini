import { useCallback, useEffect, useRef, useState } from "react";

import { toAppError } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";

export type Loaded<T> =
  | { readonly phase: "loading" }
  | { readonly phase: "ready"; readonly data: T }
  | { readonly phase: "failed"; readonly error: AppError };

export interface Load<T> {
  readonly state: Loaded<T>;
  /** True while a load runs, the first and every reload. */
  readonly busy: boolean;
  readonly reload: () => void;
}

async function settle<T>(load: () => Promise<T>): Promise<Loaded<T>> {
  try {
    return { phase: "ready", data: await load() };
  } catch (error) {
    return { phase: "failed", error: toAppError(error) };
  }
}

/**
 * Loads once when the component appears, and again on `reload`. What was loaded stays on screen
 * while a reload runs. A component that loads for one connection is keyed by the connection, so
 * another connection gets a new component and never an old answer.
 */
export function useLoad<T>(load: () => Promise<T>): Load<T> {
  const [state, setState] = useState<Loaded<T>>({ phase: "loading" });
  const [busy, setBusy] = useState(true);
  const [round, setRound] = useState(0);
  const latest = useRef(load);
  useEffect(() => {
    latest.current = load;
  });
  useEffect(() => {
    const watch = { abandoned: false };
    const run = async (): Promise<void> => {
      setBusy(true);
      const next = await settle(latest.current);
      if (watch.abandoned) {
        return;
      }
      setState(next);
      setBusy(false);
    };
    // An effect cannot be awaited; `run` settles every failure itself.
    void run();
    return (): void => {
      watch.abandoned = true;
    };
  }, [round]);
  const reload = useCallback((): void => {
    setRound((current) => current + 1);
  }, []);
  return { state, busy, reload };
}
