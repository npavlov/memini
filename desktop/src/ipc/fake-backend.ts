// A backend without Tauri, for tests: commands answer from a table, and what was asked is kept.

import { AppError } from "./app-error";
import type { Backend, CommandArguments } from "./backend";

export type FakeAnswer = (args: CommandArguments) => unknown;

export interface FakeCall {
  readonly command: string;
  readonly args: CommandArguments;
}

export interface FakeBackend extends Backend {
  /** Every command that was called, in order, with its arguments. */
  readonly calls: readonly FakeCall[];
  /** Every text that was copied, secrets included. */
  readonly copied: readonly string[];
}

export interface FakeBackendOptions {
  readonly answers: Readonly<Record<string, FakeAnswer>>;
  readonly nowMs?: number;
}

/** A command without an answer rejects as the real backend does for a command it does not allow. */
export function createFakeBackend(options: FakeBackendOptions): FakeBackend {
  const calls: FakeCall[] = [];
  const copied: string[] = [];
  const answers = new Map(Object.entries(options.answers));
  const copy = async (text: string): Promise<void> => {
    copied.push(text);
  };
  return {
    calls,
    copied,
    invoke: async (command: string, args: CommandArguments = {}): Promise<unknown> => {
      calls.push({ command, args });
      const answer = answers.get(command);
      if (answer === undefined) {
        throw new AppError({ kind: "unexpected", message: `Command ${command} not allowed` });
      }
      return answer(args);
    },
    copyText: copy,
    copySecret: copy,
    now: (): number => options.nowMs ?? 0,
  };
}
