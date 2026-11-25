import { invoke } from "@tauri-apps/api/core";
import { writeSecret, writeText } from "@tauri-apps/plugin-clipboard-manager";

import { toAppError } from "./app-error";
import type { Backend, CommandArguments } from "./backend";

async function attempt<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    throw toAppError(error);
  }
}

/** The backend of the real app: Tauri's command bridge and its clipboard plugin (write only). */
export function createTauriBackend(): Backend {
  return {
    invoke: (command: string, args?: CommandArguments): Promise<unknown> =>
      attempt(() => invoke<unknown>(command, args === undefined ? {} : { ...args })),
    copyText: (text: string): Promise<void> => attempt(() => writeText(text)),
    copySecret: (text: string): Promise<void> => attempt(() => writeSecret(text)),
    now: (): number => Date.now(),
  };
}
