import { useCallback, useRef, useState } from "react";

import { toAppError } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";

/** What an action did, in words for the user; nothing when what it did shows on the screen. */
export type ActionResult = string | null | undefined | void;

export interface Action<A> {
  /** True from the click until the action settles. Its button shows it and cannot be clicked. */
  readonly pending: boolean;
  /** The argument of the run in flight, so one action behind several buttons marks the right one. */
  readonly running: { readonly argument: A } | null;
  readonly error: AppError | null;
  /** What the last run did, when it said; null before a run, while one runs and after a failure. */
  readonly done: string | null;
  /** Runs the action. Never rejects: a failure becomes `error`. A second run while one runs is ignored. */
  readonly run: (argument: A) => Promise<void>;
  readonly clearError: () => void;
}

/** Where a run reports its life: started with an argument, ended with words or an error. */
export interface RunReport<A> {
  readonly started: (argument: A) => void;
  readonly succeeded: (done: string | null) => void;
  readonly failed: (error: AppError) => void;
  readonly settled: () => void;
}

/**
 * Runs `act` once unless a run of the same gate is in flight: the rule that an action never runs
 * twice at once lives here, not in each button. Never rejects.
 */
export async function runOnce<A>(
  gate: { current: boolean },
  act: (argument: A) => Promise<ActionResult>,
  argument: A,
  report: RunReport<A>,
): Promise<void> {
  if (gate.current) {
    return;
  }
  gate.current = true;
  report.started(argument);
  try {
    const result = await act(argument);
    report.succeeded(typeof result === "string" && result !== "" ? result : null);
  } catch (failure) {
    report.failed(toAppError(failure));
  } finally {
    gate.current = false;
    report.settled();
  }
}

/**
 * Something the user starts with a click: whether it runs, what it did, and its failure as an
 * AppError. It never runs twice at once, whatever the button does: see "Actions" in
 * .claude/rules/desktop.md.
 */
export function useAction<A = void>(act: (argument: A) => Promise<ActionResult>): Action<A> {
  const [running, setRunning] = useState<{ readonly argument: A } | null>(null);
  const [error, setError] = useState<AppError | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const gate = useRef(false);
  const run = useCallback(
    (argument: A): Promise<void> =>
      runOnce(gate, act, argument, {
        started: (started) => {
          setRunning({ argument: started });
          setError(null);
          setDone(null);
        },
        succeeded: setDone,
        failed: setError,
        settled: () => {
          setRunning(null);
        },
      }),
    [act],
  );
  const clearError = useCallback((): void => {
    setError(null);
  }, []);
  return { pending: running !== null, running, error, done, run, clearError };
}
