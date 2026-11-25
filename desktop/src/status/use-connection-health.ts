import { useCallback, useEffect, useRef } from "react";

import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { readConnectionHealth } from "./connection-health";
import type { ConnectionHealth } from "./connection-health";

/** The health of the selected connection as the app watches it: for the bar and for the screens. */
export interface HealthWatch {
  /** Null while the server is asked, and when no connection is selected. */
  readonly health: ConnectionHealth | null;
  readonly busy: boolean;
  readonly reload: () => void;
}

/** How often the app asks again while the window is in front. */
const REFRESH_MS = 60_000;

/** What makes a connection another one for its health: its server and whether it has a token. */
function watchKey(connection: Connection | null): string {
  return connection === null ? "" : `${connection.id}:${connection.url}:${String(connection.hasToken)}`;
}

/**
 * Asks about the selected connection when it is selected, when its address or its token changes,
 * and every minute. An answer is kept with the connection it is about, so the health of one
 * connection is never shown for another.
 */
export function useConnectionHealth(connection: Connection | null): HealthWatch {
  const backend = useBackend();
  const key = watchKey(connection);
  const asked = useRef(connection);
  useEffect(() => {
    asked.current = connection;
  });
  const read = useCallback(async () => {
    const about = asked.current;
    return { key, health: about === null ? null : await readConnectionHealth(backend, about) };
  }, [backend, key]);
  const loaded = useLoad(read);
  const { reload } = loaded;
  const shown = useRef(key);
  useEffect(() => {
    if (shown.current !== key) {
      shown.current = key;
      reload();
    }
  }, [key, reload]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        reload();
      }
    }, REFRESH_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [reload]);
  const answer = loaded.state.phase === "ready" ? loaded.state.data : null;
  return { health: answer?.key === key ? answer.health : null, busy: loaded.busy, reload };
}
