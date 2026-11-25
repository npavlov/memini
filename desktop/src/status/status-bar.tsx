import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { useBackend } from "../ipc/backend-context";
import { Pill } from "../ui/pill";
import { useLoad } from "../ui/use-load";
import { barItems, readConnectionHealth } from "./connection-health";
import type { ConnectionHealth } from "./connection-health";
import { StatusDetails } from "./status-details";

export interface StatusBarProps {
  /** The selected connection; null when none is. */
  readonly connection: Connection | null;
}

/** How often the bar asks again while the window is in front. */
const REFRESH_MS = 60_000;

/**
 * The bar at the bottom of the window. It is always there and says how the selected connection
 * is doing; the details of the base open above it on demand.
 */
export function StatusBar({ connection }: StatusBarProps): ReactElement {
  if (connection === null) {
    return (
      <footer className="statusbar">
        <span className="muted">No connection is selected. Add or choose one on the Connection screen.</span>
      </footer>
    );
  }
  const key = `${connection.id}:${connection.url}:${String(connection.hasToken)}`;
  return <ConnectionBar key={key} connection={connection} />;
}

interface ConnectionBarProps {
  readonly connection: Connection;
}

function ConnectionBar({ connection }: ConnectionBarProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => readConnectionHealth(backend, connection), [backend, connection]),
  );
  const [open, setOpen] = useState(false);
  const { reload } = loaded;
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
  const health = loaded.state.phase === "ready" ? loaded.state.data : null;
  return (
    <footer className="statusbar">
      {open && health !== null ? (
        <div className="statusbar-details" id="status-details" role="region" aria-label="Status details">
          <StatusDetails health={health} />
        </div>
      ) : null}
      <strong>{connection.name}</strong>
      <span className="muted statusbar-address">{connection.url}</span>
      <BarStates health={health} />
      <span className="statusbar-actions">
        <button type="button" className="button" disabled={loaded.busy} onClick={reload}>
          {loaded.busy ? "Checking…" : "Check now"}
        </button>
        <button
          type="button"
          className="button"
          aria-expanded={open}
          aria-controls="status-details"
          disabled={health === null}
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Hide details" : "Details"}
        </button>
      </span>
    </footer>
  );
}

interface BarStatesProps {
  readonly health: ConnectionHealth | null;
}

function BarStates({ health }: BarStatesProps): ReactElement {
  if (health === null) {
    return <span className="muted">Asking the server…</span>;
  }
  return (
    <>
      {barItems(health).map((item) => (
        <Pill key={item.key} tone={item.tone}>
          {item.label}
        </Pill>
      ))}
    </>
  );
}
