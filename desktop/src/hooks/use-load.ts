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
  /**
   * Loads again. Settles once what was loaded is in `state`, never rejects, so an action can wait
   * until the screen shows the answer that came after it.
   */
  readonly reload: () => Promise<void>;
}

/** A reload that waits for its answer: the round it asked for, and how to tell it. */
export interface WaitingReload {
  readonly round: number;
  readonly resolve: () => void;
}

/**
 * Splits the waiting reloads at the round that just settled: a reload asked for that round or an
 * earlier one is answered by it; one asked for a later round waits on, so an answer read before
 * the reload was asked never ends it.
 */
export function answeredBy(
  waiting: readonly WaitingReload[],
  round: number,
): { readonly answered: readonly WaitingReload[]; readonly still: WaitingReload[] } {
  return {
    answered: waiting.filter((reload) => reload.round <= round),
    still: waiting.filter((reload) => reload.round > round),
  };
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
  /** The reloads that wait for an answer, each with the round that answers it or a later one. */
  const waiting = useRef<WaitingReload[]>([]);
  const asked = useRef(0);
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
      const { answered, still } = answeredBy(waiting.current, round);
      waiting.current = still;
      for (const reload of answered) {
        reload.resolve();
      }
    };
    // An effect cannot be awaited; `run` settles every failure itself.
    void run();
    return (): void => {
      // A newer round answers the reloads that waited for this one.
      watch.abandoned = true;
    };
  }, [round]);
  useEffect(
    () => (): void => {
      // Gone from the screen: nothing will answer, so nobody waits.
      for (const reload of waiting.current.splice(0)) {
        reload.resolve();
      }
    },
    [],
  );
  const reload = useCallback(
    (): Promise<void> =>
      new Promise<void>((resolve) => {
        asked.current += 1;
        waiting.current.push({ round: asked.current, resolve });
        setRound(asked.current);
      }),
    [],
  );
  return { state, busy, reload };
}
