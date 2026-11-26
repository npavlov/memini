import { useCallback, useEffect, useRef, useState } from "react";

import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import type { AppError } from "../ipc/app-error";
import { useBackend } from "../ipc/backend-context";
import { listWorkspaceNames } from "../tokens/tokens-api";
import { currentWorkspace } from "./workspaces";

export interface WorkspaceChoice {
  /** The workspaces of the connection; null while they are asked for, or when that failed. */
  readonly names: readonly string[] | null;
  readonly error: AppError | null;
  /** The workspace every screen shows; null when there is none, or none known yet. */
  readonly current: string | null;
  readonly choose: (name: string) => void;
  /** Asks for the names again, and chooses `name` once it is there: a workspace just made. */
  readonly reload: (name?: string) => void;
}

interface Answer {
  readonly key: string;
  readonly names: readonly string[];
}

/**
 * The workspaces of the selected connection and the one chosen, which the sidebar switches and
 * every screen follows. `key` names the connection and its token: another key asks again, and
 * an answer for an older key is never shown.
 */
export function useWorkspaces(connection: Connection | null, key: string | null): WorkspaceChoice {
  const backend = useBackend();
  const id = connection?.id ?? "";
  const load = useCallback(
    async (): Promise<Answer> =>
      key === null ? { key: "", names: [] } : { key, names: await listWorkspaceNames(backend, id) },
    [backend, id, key],
  );
  const loaded = useLoad(load);
  const { reload: again } = loaded;
  const asked = useRef(key);
  useEffect(() => {
    if (asked.current !== key) {
      asked.current = key;
      again();
    }
  }, [key, again]);
  // The choice is kept per connection, so going back to one finds its workspace again.
  const [chosen, setChosen] = useState<Readonly<Record<string, string>>>({});
  const choose = useCallback(
    (name: string): void => {
      setChosen((before) => ({ ...before, [id]: name }));
    },
    [id],
  );
  const reload = useCallback(
    (name?: string): void => {
      if (name !== undefined) {
        choose(name);
      }
      again();
    },
    [again, choose],
  );
  const answer = loaded.state.phase === "ready" && loaded.state.data.key === (key ?? "") ? loaded.state.data : null;
  const names = key === null ? null : (answer?.names ?? null);
  return {
    names,
    error: loaded.state.phase === "failed" ? loaded.state.error : null,
    current: names === null ? null : currentWorkspace(names, chosen[id]),
    choose,
    reload,
  };
}
