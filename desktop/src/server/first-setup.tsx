import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  CodeBlock,
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
import { OLLAMA_DOWNLOAD, SPEEDUP } from "./host-model";
import { NOT_STOPPED, runSetup } from "./setup-run";
import type { OllamaChoice, OllamaMissing, Stopped } from "./setup-run";

export interface FirstSetupProps {
  /** Something else is running: the setup waits. */
  readonly disabled: boolean;
  readonly onView: (view: ConnectionsView) => void;
  /** Tells the screen that the setup runs, so it shows the services as they come up. */
  readonly onRunning: (running: boolean) => void;
  /** The launcher lists services already: before that, the first start is downloading. */
  readonly servicesSeen: boolean;
}

/** One run of the setup: whether to ask for another admin token, and what to do without Ollama. */
interface SetupChoice {
  readonly force: boolean;
  readonly ollama: OllamaChoice;
}

const FIRST: SetupChoice = { force: false, ollama: "ask" };

/**
 * The first setup in one button: Docker is checked, the search model settled (on a Mac, Ollama
 * on the Mac with the model downloaded), the server installed and started, the first admin token
 * created and stored in the keychain, and this computer made a connection. The token never comes
 * to this window.
 */
export function FirstSetup(props: FirstSetupProps): ReactElement {
  const { disabled, onView, onRunning, servicesSeen } = props;
  const backend = useBackend();
  const [progress, setProgress] = useState<SetupProgress>(NOT_STARTED);
  const [stopped, setStopped] = useState<Stopped>(NOT_STOPPED);
  const start = useCallback(
    async (choice: SetupChoice): Promise<void> => {
      setStopped(NOT_STOPPED);
      onRunning(true);
      try {
        const view = await runSetup(backend, {
          force: choice.force,
          ollama: choice.ollama,
          onProgress: setProgress,
          onStopped: setStopped,
        });
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
        connects this app to it. On a Mac the search model runs in Ollama on the Mac, {SPEEDUP}
        than in Docker. No terminal is needed.
      </Text>
      <StepList steps={setupSteps(progress, servicesSeen)} />
      {stopped.docker === null ? null : <DockerHelp advice={stopped.docker} />}
      {stopped.ollama === null ? null : (
        <OllamaHelp
          missing={stopped.ollama}
          disabled={!idle}
          onChoose={(ollama) => setup.run({ force: false, ollama })}
        />
      )}
      {stopped.tokenExists ? (
        <TokenExists disabled={!idle} onForce={() => setup.run({ force: true, ollama: "ask" })} />
      ) : null}
      <ErrorNotice error={setup.error} />
      <Actions>
        <Button variant="primary" disabled={!idle} onClick={() => setup.run(FIRST)}>
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

interface OllamaHelpProps {
  readonly missing: OllamaMissing;
  readonly disabled: boolean;
  readonly onChoose: (choice: OllamaChoice) => Promise<void>;
}

/**
 * No Ollama answers on this Mac. With Homebrew the step installs it after the user said so;
 * without, it names the download. Either way the setup can go on with the model in Docker, which
 * the Server screen can move to the Mac later.
 */
function OllamaHelp({ missing, disabled, onChoose }: OllamaHelpProps): ReactElement {
  return (
    <Notice tone="warn" title="Ollama is not on this Mac">
      <Text>
        The search model runs {SPEEDUP} in Ollama on the Mac than in Docker, which has no access to
        the Mac's GPU. Without Ollama the first index of a large repository can take hours.
      </Text>
      {missing.install === null || missing.install.tail.length === 0 ? null : (
        <CodeBlock label="Homebrew said" text={missing.install.tail.join("\n")} />
      )}
      {missing.brew ? null : (
        <Actions>
          <Mono>{OLLAMA_DOWNLOAD}</Mono>
          <CopyButton text={OLLAMA_DOWNLOAD} label="Copy the link" />
        </Actions>
      )}
      <Actions>
        {missing.brew ? (
          <ConfirmButton
            label="Install Ollama with Homebrew"
            question="Run brew install ollama, then brew services start ollama? It downloads about 1 GB."
            confirmLabel="Install and start it"
            disabled={disabled}
            onConfirm={() => onChoose("install")}
          />
        ) : (
          <Button disabled={disabled} onClick={() => onChoose("ask")}>
            I have installed it: go on
          </Button>
        )}
        <Button disabled={disabled} onClick={() => onChoose("skip")}>
          Go on without it
        </Button>
      </Actions>
      <Text tone="muted" small>
        Without it the model runs in Docker; the Server screen can move it to Ollama later.
      </Text>
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
