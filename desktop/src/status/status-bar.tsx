import { useState } from "react";
import type { ReactElement } from "react";

import { Button, IconButton, Mono, Pill, StatusBarActions, StatusBarFrame, Text } from "../components";
import type { Connection } from "../connections/connections";
import { barItems } from "./connection-health";
import type { ConnectionHealth } from "./connection-health";
import { StatusDetails } from "./status-details";
import type { HealthWatch } from "./use-connection-health";

export interface StatusBarProps {
  /** The selected connection; null when none is. */
  readonly connection: Connection | null;
  /** Its health, as the app watches it. */
  readonly watch: HealthWatch;
}

const DETAILS_ID = "status-details";

/**
 * The bar at the bottom of the window. It is always there and says how the selected connection
 * is doing; the details of the base open above it on demand.
 */
export function StatusBar({ connection, watch }: StatusBarProps): ReactElement {
  if (connection === null) {
    return (
      <StatusBarFrame>
        <Text tone="muted" inline>
          No connection is selected. Add or choose one on the Connection screen.
        </Text>
      </StatusBarFrame>
    );
  }
  // Keyed by the connection: the details of one are not left open over another.
  return <ConnectionBar key={connection.id} connection={connection} watch={watch} />;
}

interface ConnectionBarProps {
  readonly connection: Connection;
  readonly watch: HealthWatch;
}

function ConnectionBar({ connection, watch }: ConnectionBarProps): ReactElement {
  const [open, setOpen] = useState(false);
  const { health, busy, reload } = watch;
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
          label={busy ? "Checking the connection" : "Check the connection now"}
          icon="refresh"
          disabled={busy}
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
