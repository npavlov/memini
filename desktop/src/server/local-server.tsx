import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, ConfirmButton, ErrorNotice, Notice, Panel, Text } from "../components";
import type { ConnectionsView } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { describePhase, serverPhase } from "./launcher";
import type { LauncherChange, LauncherStatus } from "./launcher";
import { changeServer, readLauncherStatus } from "./launcher-api";
import { LocalConnection } from "./local-connection";
import { ServerFacts } from "./server-facts";

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
  mcp_on: "The MCP endpoint was switched on.",
  mcp_off: "The MCP endpoint was switched off.",
};

export function LocalServer({ view, info, onView }: LocalServerProps): ReactElement {
  const backend = useBackend();
  const status = useLoad(useCallback(() => readLauncherStatus(backend), [backend]));
  const [outcome, setOutcome] = useState<Outcome>(null);
  const { reload } = status;
  const change = useCallback(
    async (kind: LauncherChange): Promise<void> => {
      setOutcome(null);
      const result = await changeServer(backend, kind);
      setOutcome({
        ok: result.ok,
        text: result.ok ? DONE[kind] : (result.error ?? "The launcher reported a failure."),
      });
      reload();
    },
    [backend, reload],
  );
  const changing = useAction(change);
  const busy = status.busy || changing.pending;
  return (
    <>
      <Panel
        title="Local server"
        actions={
          <Button disabled={busy} onClick={reload}>
            {status.busy ? "Refreshing…" : "Refresh"}
          </Button>
        }
      >
        {status.state.phase === "loading" ? <Text tone="muted">Asking the launcher…</Text> : null}
        {status.state.phase === "failed" ? <ErrorNotice error={status.state.error} /> : null}
        {status.state.phase === "ready" ? (
          <ServerControls status={status.state.data} busy={busy} onChange={changing.run} />
        ) : null}
        {changing.pending ? <ChangeInProgress /> : null}
        {outcome === null ? null : (
          <Notice tone={outcome.ok ? "good" : "bad"} title={outcome.text} />
        )}
        <ErrorNotice error={changing.error} />
      </Panel>
      {status.state.phase === "ready" ? (
        <>
          <ServerFacts status={status.state.data} />
          <LocalConnection status={status.state.data} view={view} info={info} onView={onView} />
        </>
      ) : null}
    </>
  );
}

function ChangeInProgress(): ReactElement {
  return (
    <Notice tone="info" title="The launcher is working…">
      <Text>
        The first start downloads the application, the database and the search model, which can
        take a long time. Keep the app open: closing it stops the launcher.
      </Text>
    </Notice>
  );
}

interface ServerControlsProps {
  readonly status: LauncherStatus;
  readonly busy: boolean;
  readonly onChange: (change: LauncherChange) => Promise<void>;
}

function ServerControls({ status, busy, onChange }: ServerControlsProps): ReactElement {
  const phase = serverPhase(status);
  const summary = describePhase(phase);
  const stopped = phase === "not_installed" || phase === "stopped";
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
        {stopped ? (
          <Button variant="primary" disabled={busy} onClick={() => onChange("start")}>
            {phase === "not_installed" ? "Install and start" : "Start"}
          </Button>
        ) : null}
        {status.running ? (
          <ConfirmButton
            label="Stop"
            question="Stop the server? Agents connected to it lose their connection."
            confirmLabel="Stop the server"
            disabled={busy}
            onConfirm={() => onChange("stop")}
          />
        ) : null}
      </Actions>
    </>
  );
}
