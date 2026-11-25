import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Button, ErrorNotice, Notice, Panel, Text } from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { displayText } from "../text/display-text";
import { describeReadiness, describeTokenCheck } from "./connection-check";
import type { ConnectionCheck } from "./connection-check";
import type { Connection } from "./connections";
import { checkConnection } from "./connections-api";

export interface ConnectionCheckerProps {
  readonly connection: Connection;
}

/** Asks the server for its version and readiness, then whether the stored token may read. */
export function ConnectionChecker({ connection }: ConnectionCheckerProps): ReactElement {
  const backend = useBackend();
  const [result, setResult] = useState<ConnectionCheck | null>(null);
  const check = useCallback(async (): Promise<void> => {
    setResult(null);
    setResult(await checkConnection(backend, connection.id));
  }, [backend, connection.id]);
  const checking = useAction(check);
  return (
    <Panel
      title="Check"
      actions={
        <Button disabled={checking.pending} onClick={() => checking.run()}>
          {checking.pending ? "Checking…" : "Check the connection"}
        </Button>
      }
    >
      {result === null && checking.error === null ? (
        <Text tone="muted">
          Asks the server for its version and readiness, then whether it accepts the stored token.
        </Text>
      ) : null}
      <ErrorNotice error={checking.error} />
      {result === null ? null : <CheckFindings check={result} />}
    </Panel>
  );
}

interface CheckFindingsProps {
  readonly check: ConnectionCheck;
}

function CheckFindings({ check }: CheckFindingsProps): ReactElement {
  const readiness = describeReadiness(check);
  const token = describeTokenCheck(check);
  const detail = check.token === "accepted" ? "" : displayText(check.detail);
  return (
    <>
      <Notice tone={readiness.tone} title={readiness.text} />
      <Notice tone={token.tone} title={token.text}>
        {detail === "" ? null : (
          <Text tone="muted" small>
            The server said: {detail}
          </Text>
        )}
      </Notice>
    </>
  );
}
