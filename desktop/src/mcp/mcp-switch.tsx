import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Actions, ConfirmButton, ErrorNotice, Notice, Text } from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { changeServer } from "../server/launcher-api";

export interface McpSwitchProps {
  readonly on: boolean;
  readonly onChanged: () => void;
}

/**
 * Switches the MCP endpoint of the server on this computer through the launcher. The setting
 * takes effect with a restart of the service, so the user is asked first either way.
 */
export function McpSwitch({ on, onChanged }: McpSwitchProps): ReactElement {
  const backend = useBackend();
  const [refusal, setRefusal] = useState<string | null>(null);
  const flip = useCallback(async (): Promise<void> => {
    setRefusal(null);
    const result = await changeServer(backend, on ? "mcp_off" : "mcp_on");
    if (!result.ok) {
      setRefusal(result.error ?? "The launcher reported a failure.");
    }
    onChanged();
  }, [backend, on, onChanged]);
  const flipping = useAction(flip);
  return (
    <>
      <Actions>
        <ConfirmButton
          label={on ? "Switch MCP off" : "Switch MCP on"}
          question={
            on
              ? "Switch MCP off? The server restarts and agents lose their connection."
              : "Switch MCP on? The server restarts."
          }
          confirmLabel={on ? "Switch off" : "Switch on"}
          disabled={flipping.pending}
          onConfirm={() => flipping.run()}
        />
        {flipping.pending ? (
          <Text tone="muted" inline>
            The launcher is working…
          </Text>
        ) : null}
      </Actions>
      {refusal === null ? null : <Notice tone="bad" title={refusal} />}
      <ErrorNotice error={flipping.error} />
    </>
  );
}
