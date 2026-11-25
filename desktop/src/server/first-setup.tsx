import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  ConfirmButton,
  CopyButton,
  ErrorNotice,
  Mono,
  Notice,
  Panel,
  StepList,
  Text,
} from "../components";
import type { ConnectionsView } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { NOT_STARTED, setupSteps } from "./setup";
import type { DockerAdvice, SetupProgress } from "./setup";
import { NOT_STOPPED, runSetup } from "./setup-run";
import type { Stopped } from "./setup-run";

export interface FirstSetupProps {
  /** Something else is running: the setup waits. */
  readonly disabled: boolean;
  readonly onView: (view: ConnectionsView) => void;
  /** Tells the screen that the setup runs, so it shows the services as they come up. */
  readonly onRunning: (running: boolean) => void;
  /** The launcher lists services already: before that, the first start is downloading. */
  readonly servicesSeen: boolean;
}

/**
 * The first setup in one button: Docker is checked, the server installed and started, the first
 * admin token created and stored in the keychain, and this computer made a connection. The token
 * never comes to this window.
 */
export function FirstSetup(props: FirstSetupProps): ReactElement {
  const { disabled, onView, onRunning, servicesSeen } = props;
  const backend = useBackend();
  const [progress, setProgress] = useState<SetupProgress>(NOT_STARTED);
  const [stopped, setStopped] = useState<Stopped>(NOT_STOPPED);
  const start = useCallback(
    async (force: boolean): Promise<void> => {
      setStopped(NOT_STOPPED);
      onRunning(true);
      try {
        const view = await runSetup(backend, { force, onProgress: setProgress, onStopped: setStopped });
        if (view !== null) {
          onView(view);
        }
      } finally {
        onRunning(false);
      }
    },
    [backend, onRunning, onView],
  );
  const setup = useAction(start);
  const idle = !setup.pending && !disabled;
  return (
    <Panel title="Set up Memini on this computer">
      <Text>
        One step installs the server with Docker, starts it, creates its first admin token and
        connects this app to it. No terminal is needed.
      </Text>
      <StepList steps={setupSteps(progress, servicesSeen)} />
      {stopped.docker === null ? null : <DockerHelp advice={stopped.docker} />}
      {stopped.tokenExists ? <TokenExists disabled={!idle} onForce={() => setup.run(true)} /> : null}
      <ErrorNotice error={setup.error} />
      <Actions>
        <Button variant="primary" disabled={!idle} onClick={() => setup.run(false)}>
          {progress.at === null ? "Set up" : "Set up again"}
        </Button>
        {setup.pending ? (
          <Text tone="muted" inline>
            Keep the app open: closing it stops the setup.
          </Text>
        ) : null}
      </Actions>
    </Panel>
  );
}

interface DockerHelpProps {
  readonly advice: DockerAdvice;
}

function DockerHelp({ advice }: DockerHelpProps): ReactElement {
  return (
    <Notice tone="warn" title={advice.title}>
      <Text>{advice.text}</Text>
      {advice.page === null ? null : (
        <Actions>
          <Mono>{advice.page}</Mono>
          <CopyButton text={advice.page} label="Copy the link" />
        </Actions>
      )}
    </Notice>
  );
}

interface TokenExistsProps {
  readonly disabled: boolean;
  readonly onForce: () => Promise<void>;
}

function TokenExists({ disabled, onForce }: TokenExistsProps): ReactElement {
  return (
    <Notice tone="warn" title="This server has an admin token already">
      <Text>
        It was set up before. If you have that token, store it on the Connection screen. If it is
        lost, the app can create another one; the old one keeps working until it is revoked on the
        Tokens screen.
      </Text>
      <ConfirmButton
        label="Create another admin token"
        question="Create a second admin token for this server?"
        confirmLabel="Create it"
        disabled={disabled}
        onConfirm={onForce}
      />
    </Notice>
  );
}
