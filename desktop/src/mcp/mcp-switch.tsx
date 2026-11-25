import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { ErrorNotice, Notice, Text, Toggle } from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { changeServer } from "../server/launcher-api";

export interface McpSwitchProps {
  readonly on: boolean;
  /** Why the switch cannot be worked from here; null when it can. */
  readonly unavailable: string | null;
  /** The screen is reading the state again: the switch waits for it. */
  readonly reading: boolean;
  /** Called after a change, to read the state again. */
  readonly onChanged: () => void;
}

/**
 * Switches the MCP endpoint of the server on this computer through the launcher. The change is
 * made at once and restarts the service; what the switch then shows is read from the server
 * again, not assumed.
 */
export function McpSwitch({ on, unavailable, reading, onChanged }: McpSwitchProps): ReactElement {
  const backend = useBackend();
  const [refusal, setRefusal] = useState<string | null>(null);
  const flip = useCallback(
    async (wanted: boolean): Promise<void> => {
      setRefusal(null);
      const result = await changeServer(backend, wanted ? "mcp_on" : "mcp_off");
      if (!result.ok) {
        setRefusal(result.error ?? "The launcher reported a failure.");
      }
      onChanged();
    },
    [backend, onChanged],
  );
  const flipping = useAction(flip);
  const working = flipping.pending ? "The launcher is switching it; the server restarts." : null;
  return (
    <>
      <Toggle
        label="MCP endpoint"
        on={on}
        busy={working ?? (reading ? "Reading the state again…" : null)}
        unavailable={unavailable}
        onChange={(wanted) => flipping.run(wanted)}
      />
      {unavailable === null ? (
        <Text tone="muted" small>
          Switching restarts the server. While it is off, agents cannot connect.
        </Text>
      ) : null}
      {refusal === null ? null : <Notice tone="bad" title={refusal} />}
      <ErrorNotice error={flipping.error} />
    </>
  );
}
