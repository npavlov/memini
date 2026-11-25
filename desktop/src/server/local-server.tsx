import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, ConfirmButton, ErrorNotice, Notice, Panel, Text } from "../components";
import { localConnection } from "../connections/connections";
import type { ConnectionsView } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import type { Action } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Load } from "../hooks/use-load";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { FirstSetup } from "./first-setup";
import { describePhase, serverPhase } from "./launcher";
import type { LauncherChange, LauncherStatus } from "./launcher";
import { changeServer, readLauncherStatus } from "./launcher-api";
import { LocalConnection } from "./local-connection";
import { ServerFacts } from "./server-facts";
import { busyReason, needsSetup } from "./setup";

export interface LocalServerProps {
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
}

/** What the last change did, when the launcher answered that it failed. */
type Outcome = { readonly ok: boolean; readonly text: string } | null;

const DONE: Readonly<Record<LauncherChange, string>> = {
  start: "The server was started.",
  stop: "The server was stopped.",
  restart: "The server was restarted.",
  mcp_on: "The MCP endpoint was switched on.",
  mcp_off: "The MCP endpoint was switched off.",
};

/** How often the status is read again while the launcher works, to show the services come up. */
const PROGRESS_MS = 4000;

interface ServerControl {
  readonly status: Load<LauncherStatus>;
  /** What runs now; null when nothing does. */
  readonly working: LauncherChange | "setup" | null;
  readonly outcome: Outcome;
  readonly changing: Action<LauncherChange>;
  readonly onSetup: (running: boolean) => void;
}

/**
 * The state of the server on this computer and the changes to it. One change runs at a time.
 * While one runs, the status is read again every few seconds, so the services are seen to come
 * up; after it, the status is read once more, because a restart can move the server's port.
 */
function useServerControl(): ServerControl {
  const backend = useBackend();
  const status = useLoad(useCallback(() => readLauncherStatus(backend), [backend]));
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [working, setWorking] = useState<LauncherChange | "setup" | null>(null);
  const { reload } = status;
  const change = useCallback(
    async (kind: LauncherChange): Promise<void> => {
      setOutcome(null);
      setWorking(kind);
      try {
        const result = await changeServer(backend, kind);
        const failed = result.error ?? "The launcher reported a failure.";
        setOutcome({ ok: result.ok, text: result.ok ? DONE[kind] : failed });
      } finally {
        setWorking(null);
        reload();
      }
    },
    [backend, reload],
  );
  const changing = useAction(change);
  const onSetup = useCallback(
    (running: boolean): void => {
      setWorking(running ? "setup" : null);
      reload();
    },
    [reload],
  );
  useEffect(() => {
    if (working === null) {
      return undefined;
    }
    const timer = window.setInterval(reload, PROGRESS_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [working, reload]);
  return { status, working, outcome, changing, onSetup };
}

export function LocalServer({ view, info, onView }: LocalServerProps): ReactElement {
  const { status, working, outcome, changing, onSetup } = useServerControl();
  const loaded = status.state.phase === "ready" ? status.state.data : null;
  const reason = busyReason(working);
  return (
    <>
      {loaded !== null && needsSetup(loaded, localConnection(view)) ? (
        <FirstSetup disabled={working !== null && working !== "setup"} onView={onView} onRunning={onSetup} />
      ) : null}
      <Panel
        title="Local server"
        actions={
          <Button disabled={status.busy} onClick={status.reload}>
            {status.busy ? "Refreshing…" : "Refresh"}
          </Button>
        }
      >
        {status.state.phase === "loading" ? <Text tone="muted">Asking the launcher…</Text> : null}
        {status.state.phase === "failed" ? <ErrorNotice error={status.state.error} /> : null}
        {loaded === null ? null : (
          <ServerControls status={loaded} busy={working !== null} onChange={changing.run} />
        )}
        {reason === null ? null : <Notice tone="info" title={reason} />}
        {outcome === null ? null : <Notice tone={outcome.ok ? "good" : "bad"} title={outcome.text} />}
        <ErrorNotice error={changing.error} />
      </Panel>
      {loaded === null ? null : (
        <>
          <ServerFacts status={loaded} />
          <LocalConnection status={loaded} view={view} info={info} onView={onView} />
        </>
      )}
    </>
  );
}

interface ServerControlsProps {
  readonly status: LauncherStatus;
  /** Something runs: every button waits, and the screen says why. */
  readonly busy: boolean;
  readonly onChange: (change: LauncherChange) => Promise<void>;
}

function ServerControls({ status, busy, onChange }: ServerControlsProps): ReactElement {
  const phase = serverPhase(status);
  const summary = describePhase(phase);
  const startable = status.dockerAvailable && status.installed && !status.running;
  return (
    <>
      <Notice tone={summary.tone} title={summary.title}>
        <Text>{summary.text}</Text>
        {phase === "no_docker" && status.dockerError !== null ? (
          <Text tone="muted" small>
            The launcher said: {status.dockerError}
          </Text>
        ) : null}
      </Notice>
      <Actions>
        {startable ? (
          <Button variant="primary" disabled={busy} onClick={() => onChange("start")}>
            Start
          </Button>
        ) : null}
        {status.running ? (
          <>
            <ConfirmButton
              label="Restart"
              question="Restart the server? Agents connected to it lose their connection for a moment."
              confirmLabel="Restart the server"
              disabled={busy}
              onConfirm={() => onChange("restart")}
            />
            <ConfirmButton
              label="Stop"
              question="Stop the server? Agents connected to it lose their connection."
              confirmLabel="Stop the server"
              disabled={busy}
              onConfirm={() => onChange("stop")}
            />
          </>
        ) : null}
      </Actions>
    </>
  );
}
