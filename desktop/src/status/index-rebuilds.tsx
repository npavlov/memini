import { useCallback, useEffect } from "react";
import type { ReactElement } from "react";

import { Notice, ProgressBar, Stack, Text } from "../components";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { describeRebuild, readServiceStatus, rebuildsOf } from "./status";
import type { WorkspaceRebuild } from "./status";

/** How often the status is read again while a rebuild waits or runs. */
const REBUILD_POLL_MS = 5000;

export interface IndexRebuildsProps {
  readonly rebuilds: readonly WorkspaceRebuild[];
}

/**
 * The rebuilds of the search index the server runs after an upgrade, a bar for each workspace.
 * Search in a workspace is refused until its rebuild is done, so the screen says why and how far.
 */
export function IndexRebuilds({ rebuilds }: IndexRebuildsProps): ReactElement | null {
  if (rebuilds.length === 0) {
    return null;
  }
  return (
    <Notice tone="info" title="The search index is being rebuilt after an update">
      <Text>
        The new version reads code differently, so the server brings each workspace&apos;s index up
        to date by itself. Search in a workspace is back when its bar is full; nothing else is
        needed.
      </Text>
      <Stack>
        {rebuilds.map(({ workspace, rebuild }) => (
          <ProgressBar
            key={workspace}
            label={`Rebuild of the index of ${workspace}`}
            value={rebuild.recordsDone ?? 0}
            max={rebuild.state === "running" ? (rebuild.recordsTotal ?? 0) : 0}
            detail={`${workspace}: ${describeRebuild(rebuild)}`}
          />
        ))}
      </Stack>
    </Notice>
  );
}

export interface ConnectionRebuildsProps {
  /** The saved connection whose server is asked. */
  readonly connectionId: string;
}

/**
 * The rebuilds of the server of a connection, read from its status, and read again every few
 * seconds while one waits or runs. A status the token may not read shows nothing.
 */
export function ConnectionRebuilds({ connectionId }: ConnectionRebuildsProps): ReactElement | null {
  const backend = useBackend();
  const load = useLoad(
    useCallback(async () => rebuildsOf(await readServiceStatus(backend, connectionId)), [backend, connectionId]),
  );
  const rebuilds = load.state.phase === "ready" ? load.state.data : [];
  const running = rebuilds.length > 0;
  const { reload } = load;
  useEffect(() => {
    if (!running) {
      return undefined;
    }
    const timer = window.setInterval(reload, REBUILD_POLL_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [running, reload]);
  return <IndexRebuilds rebuilds={rebuilds} />;
}
