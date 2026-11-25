import { useCallback, useEffect } from "react";
import type { ReactElement } from "react";

import { Actions, Button, CodeBlock, ConfirmButton, CopyButton, ErrorNotice, Mono, Notice, Text } from "../components";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { OLLAMA_DOWNLOAD, SPEEDUP, describePull, installOllama, pullHostModel, readHostModel } from "./host-model";
import type { HostModel, Runtime } from "./host-model";
import { setRuntime } from "./indexing";
import type { IndexingAnswer } from "./indexing";

export interface ModelPlaceProps {
  readonly runtime: Runtime;
  /** The launcher's answer after the model moved. */
  readonly onMoved: (answer: IndexingAnswer) => void;
}

/** How often Ollama is asked how far a download or an installation is. */
const POLL_MS = 1000;

/**
 * Where the search model of the server on this computer runs. On a Mac it belongs in Ollama on
 * the Mac: Docker there has no GPU. Moving it downloads the model first when Ollama lacks it, with
 * the progress shown; Homebrew installs Ollama when the user says so.
 */
export function ModelPlace({ runtime, onMoved }: ModelPlaceProps): ReactElement | null {
  const backend = useBackend();
  const host = useLoad(useCallback(() => readHostModel(backend), [backend]));
  const { reload } = host;
  const move = useCallback(
    async (target: Runtime): Promise<void> => {
      const now = await readHostModel(backend);
      if (target === "host" && !now.hasModel) {
        const pulled = await pullHostModel(backend);
        if (pulled.error !== null) {
          throw new Error(pulled.error);
        }
      }
      onMoved(await setRuntime(backend, target));
    },
    [backend, onMoved],
  );
  const moving = useAction(move);
  const install = useCallback(async (): Promise<void> => {
    await installOllama(backend);
    reload();
  }, [backend, reload]);
  const installing = useAction(install);
  const busy = moving.pending || installing.pending;
  useEffect(() => {
    if (!busy) {
      return undefined;
    }
    const timer = window.setInterval(reload, POLL_MS);
    return (): void => {
      window.clearInterval(timer);
    };
  }, [busy, reload]);
  const found = host.state.phase === "ready" ? host.state.data : null;
  if (found === null || !found.supported) {
    return null;
  }
  return (
    <>
      {runtime === "host" ? (
        <OnTheMac disabled={busy} onMove={() => moving.run("container")} />
      ) : (
        <InDocker host={found} disabled={busy} onMove={() => moving.run("host")} onInstall={() => installing.run()} />
      )}
      {busy && found.pull !== null && !found.pull.done ? (
        <Text tone="muted">{describePull(found.pull, found.model)}</Text>
      ) : null}
      {installing.pending ? <Text tone="muted">Homebrew is installing Ollama…</Text> : null}
      {found.install !== null && !found.install.ok && found.install.tail.length > 0 ? (
        <CodeBlock label="Homebrew said" text={found.install.tail.join("\n")} />
      ) : null}
      <ErrorNotice error={moving.error} />
      <ErrorNotice error={installing.error} />
    </>
  );
}

interface OnTheMacProps {
  readonly disabled: boolean;
  readonly onMove: () => Promise<void>;
}

function OnTheMac({ disabled, onMove }: OnTheMacProps): ReactElement {
  return (
    <Notice tone="good" title="The search model runs in Ollama on this Mac">
      <Text>It embeds on the Mac's GPU, {SPEEDUP} than in Docker. Keep Ollama running.</Text>
      <ConfirmButton
        label="Run it in Docker instead"
        question="Run the search model in Docker? It is much slower there, as Docker on a Mac has no GPU."
        confirmLabel="Run it in Docker"
        disabled={disabled}
        onConfirm={onMove}
      />
    </Notice>
  );
}

interface InDockerProps {
  readonly host: HostModel;
  readonly disabled: boolean;
  readonly onMove: () => Promise<void>;
  readonly onInstall: () => Promise<void>;
}

function InDocker({ host, disabled, onMove, onInstall }: InDockerProps): ReactElement {
  if (host.found) {
    return (
      <Notice tone="warn" title="The search model runs in Docker, without the Mac's GPU">
        <Text>
          Ollama {host.version ?? ""} answers on this Mac and embeds {SPEEDUP}.
          {host.hasModel ? "" : ` Moving there downloads ${host.model} first.`} The index stays as it is.
        </Text>
        <Actions>
          <Button variant="primary" disabled={disabled} onClick={onMove}>
            {disabled ? "Moving…" : "Use Ollama on this Mac"}
          </Button>
        </Actions>
      </Notice>
    );
  }
  return (
    <Notice tone="warn" title="The search model runs in Docker, without the Mac's GPU">
      <Text>
        Ollama on the Mac embeds {SPEEDUP}. Install it, then move the model there with one click.
      </Text>
      <Actions>
        {host.brew ? (
          <ConfirmButton
            label="Install Ollama with Homebrew"
            question="Run brew install ollama, then brew services start ollama? It downloads about 1 GB."
            confirmLabel="Install and start it"
            disabled={disabled}
            onConfirm={onInstall}
          />
        ) : (
          <>
            <Mono>{OLLAMA_DOWNLOAD}</Mono>
            <CopyButton text={OLLAMA_DOWNLOAD} label="Copy the link" />
          </>
        )}
      </Actions>
    </Notice>
  );
}
