// Where the launcher says the local server answers, against what the saved connection uses.
// Recreating the service, which switching the MCP endpoint does, can change the published port,
// so the two can drift apart. The app reports the difference and the user decides: the launcher's
// output never changes a connection by itself.

import { changesOrigin, formOf, isLoopbackAddress } from "../connections/connections";
import type { Connection } from "../connections/connections";
import type { LauncherStatus } from "./launcher";

/** The address the launcher reports, when it is on this computer; no other address is offered. */
export function reportedLocalAddress(status: LauncherStatus | null): string | null {
  const reported = status?.apiUrl ?? null;
  return reported !== null && isLoopbackAddress(reported) ? reported : null;
}

/** The launcher's address when the local connection uses another one; null when they agree. */
export function movedLocalAddress(
  connection: Connection,
  status: LauncherStatus | null,
): string | null {
  const reported = connection.kind === "local" ? reportedLocalAddress(status) : null;
  if (reported === null) {
    return null;
  }
  return changesOrigin({ ...formOf(connection), url: reported }, connection) ? reported : null;
}
