import { useCallback, useEffect, useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, ConfirmButton, ErrorNotice, Mono, Notice, Panel, Text } from "../components";
import { localConnection } from "../connections/connections";
import type { Connection, ConnectionsView } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import type { Action } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Load } from "../hooks/use-load";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { FirstSetup } from "./first-setup";
import { GitAccessPanel } from "./git-access";
import { describePhase, serverPhase } from "./launcher";
import type { LauncherChange, LauncherStatus } from "./launcher";
import { changeServer, readLauncherStatus, removeServer } from "./launcher-api";
import { LocalConnection } from "./local-connection";
import { ServerFacts } from "./server-facts";
import { busyReason, localSetup } from "./setup";
import type { ServerWork } from "./setup";

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

const REMOVED =
  "The server was removed with its data, and the connection to it was forgotten. Docker keeps the downloaded images.";

/** How often the status is read again while the launcher works, to show the services come up. */
const PROGRESS_MS = 4000;

interface ServerControl {
  readonly status: Load<LauncherStatus>;
  /** What runs now; null when nothing does. */
  readonly working: ServerWork | null;
  readonly outcome: Outcome;
  readonly changing: Action<LauncherChange>;
  readonly removing: Action<void>;
  readonly onSetup: (running: boolean) => void;
}

/**
 * The state of the server on this computer and the changes to it. One change runs at a time.
 * While one runs, the status is read again every few seconds, so the services are seen to come
 * up; after it, the status is read once more, because a restart can move the server's port.
 */
function useServerControl(onView: (view: ConnectionsView) => void): ServerControl {
  const backend = useBackend();
  const status = useLoad(useCallback(() => readLauncherStatus(backend), [backend]));
  const [outcome, setOutcome] = useState<Outcome>(null);
  const [working, setWorking] = useState<ServerWork | null>(null);
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
  const remove = useCallback(async (): Promise<void> => {
    setOutcome(null);
    setWorking("uninstall");
    try {
      const result = await removeServer(backend);
      onView(result.view);
      const failed = result.error ?? "The launcher could not remove the server.";
      setOutcome({ ok: result.ok, text: result.ok ? REMOVED : failed });
    } finally {
      setWorking(null);
      reload();
    }
  }, [backend, onView, reload]);
  const removing = useAction(remove);
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
  return { status, working, outcome, changing, removing, onSetup };
}

export function LocalServer({ view, info, onView }: LocalServerProps): ReactElement {
  const { status, working, outcome, changing, removing, onSetup } = useServerControl(onView);
  const loaded = status.state.phase === "ready" ? status.state.data : null;
  const local = localConnection(view);
  const offered = loaded === null ? null : localSetup(loaded, local);
  const reason = busyReason(working);
  return (
    <>
      {offered === "setup" ? (
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
        {offered === "foreign" && local !== null ? <ForeignServer connection={local} /> : null}
        {loaded === null || offered === "foreign" ? null : (
          <ServerControls
            status={loaded}
            busy={working !== null}
            onChange={changing.run}
            onRemove={() => removing.run()}
          />
        )}
        {reason === null ? null : <Notice tone="info" title={reason} />}
        {outcome === null ? null : <Notice tone={outcome.ok ? "good" : "bad"} title={outcome.text} />}
        <ErrorNotice error={changing.error} />
        <ErrorNotice error={removing.error} />
      </Panel>
      {loaded === null || offered === "foreign" ? null : (
        <>
          <ServerFacts status={loaded} />
          <LocalConnection status={loaded} view={view} info={info} onView={onView} />
          {loaded.installed ? <GitAccessPanel /> : null}
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
  readonly onRemove: () => Promise<void>;
}

/**
 * The local connection answers with a token, but the launcher manages no server: it was set up
 * another way, so the app neither installs a second one nor starts, stops or removes this one.
 */
function ForeignServer({ connection }: { readonly connection: Connection }): ReactElement {
  return (
    <Notice tone="info" title="This server was not installed by this app">
      <Text>
        The connection “{connection.name}” uses <Mono>{connection.url}</Mono> and holds a token, so
        its data is on the other screens. The server was set up another way, for example with
        Docker Compose by hand, so the app cannot start, stop or remove it.
      </Text>
      <Text tone="muted" small>
        To let the app install and manage a server of its own, delete that connection on the
        Connections screen; the setup is offered here then.
      </Text>
    </Notice>
  );
}

function ServerControls({ status, busy, onChange, onRemove }: ServerControlsProps): ReactElement {
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
        {status.installed ? (
          <ConfirmButton
            label="Remove server…"
            question="Remove the server from this computer? Its containers and all its data are deleted: every workspace, source, record and token, and the search model. The connection to it and its token are forgotten, and agents set up for it stop working. Docker keeps the downloaded images. This cannot be undone."
            confirmLabel="Remove the server and its data"
            disabled={busy || !status.dockerAvailable}
            onConfirm={onRemove}
          />
        ) : null}
      </Actions>
    </>
  );
}
