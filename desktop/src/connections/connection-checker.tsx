import { useCallback } from "react";
import type { ReactElement } from "react";

import { Button, ErrorNotice, Panel, StepList, Text } from "../components";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { checkSteps } from "./connection-check";
import type { Connection } from "./connections";
import { testConnection } from "./connections-api";

export interface ConnectionCheckerProps {
  readonly connection: Connection;
}

/**
 * Tests a connection and shows each step: the address, the version, the token, whether the
 * server accepts it, and its scopes. It runs when it appears, which is when a connection is
 * selected or saved or its token changes, and again on the button.
 */
export function ConnectionChecker({ connection }: ConnectionCheckerProps): ReactElement {
  const backend = useBackend();
  const id = connection.id;
  const tested = useLoad(useCallback(() => testConnection(backend, id), [backend, id]));
  return (
    <Panel
      title="Connection test"
      actions={
        <Button disabled={tested.busy} onClick={tested.reload}>
          {tested.busy ? "Testing…" : "Test connection"}
        </Button>
      }
    >
      {tested.state.phase === "loading" ? <Text tone="muted">Testing the connection…</Text> : null}
      {tested.state.phase === "failed" ? <ErrorNotice error={tested.state.error} /> : null}
      {tested.state.phase === "ready" ? <StepList steps={checkSteps(tested.state.data)} /> : null}
    </Panel>
  );
}
