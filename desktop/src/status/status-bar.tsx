import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { Button, IconButton, Mono, Pill, StatusBarActions, StatusBarFrame, Text } from "../components";
import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { barItems, readConnectionHealth } from "./connection-health";
import type { ConnectionHealth } from "./connection-health";
import { StatusDetails } from "./status-details";

export interface StatusBarProps {
  /** The selected connection; null when none is. */
  readonly connection: Connection | null;
}

/** How often the bar asks again while the window is in front. */
const REFRESH_MS = 60_000;
const DETAILS_ID = "status-details";

/**
 * The bar at the bottom of the window. It is always there and says how the selected connection
 * is doing; the details of the base open above it on demand.
 */
export function StatusBar({ connection }: StatusBarProps): ReactElement {
  if (connection === null) {
    return (
      <StatusBarFrame>
        <Text tone="muted" inline>
          No connection is selected. Add or choose one on the Connection screen.
        </Text>
      </StatusBarFrame>
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
  const details = open && health !== null ? <StatusDetails health={health} /> : null;
  return (
    <StatusBarFrame details={details} detailsId={DETAILS_ID}>
      <strong>{connection.name}</strong>
      <Text tone="muted" inline>
        <Mono>{connection.url}</Mono>
      </Text>
      <BarStates health={health} />
      <StatusBarActions>
        <IconButton
          label={loaded.busy ? "Checking the connection" : "Check the connection now"}
          icon="refresh"
          disabled={loaded.busy}
          onClick={reload}
        />
        <Button
          expanded={open}
          controls={DETAILS_ID}
          disabled={health === null}
          onClick={() => {
            setOpen(!open);
          }}
        >
          {open ? "Hide details" : "Details"}
        </Button>
      </StatusBarActions>
    </StatusBarFrame>
  );
}

interface BarStatesProps {
  readonly health: ConnectionHealth | null;
}

function BarStates({ health }: BarStatesProps): ReactElement {
  if (health === null) {
    return (
      <Text tone="muted" inline>
        Asking the server…
      </Text>
    );
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
