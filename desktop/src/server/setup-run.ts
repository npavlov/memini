// The first setup as a sequence of commands of the Rust side: read the status, settle where the
// search model runs, start the server if it is not running, then let Rust take the first admin
// token into the keychain. Each step stops the run when it does not pass, and says why.

import type { ConnectionsView } from "../connections/connections";
import type { Backend } from "../ipc/backend";
import { describePull, installOllama, modelPlan, pullHostModel, readHostModel } from "./host-model";
import type { InstallOutcome, PullProgress } from "./host-model";
import { changeServer, readLauncherStatus, setupToken } from "./launcher-api";
import { dockerAdvice, tokenExists } from "./setup";
import type { DockerAdvice, SetupProgress } from "./setup";

/** No Ollama answers on this Mac: the user says whether to install it or go on without it. */
export interface OllamaMissing {
  /** Homebrew can install it from the step. */
  readonly brew: boolean;
  /** What an installation that did not work printed last. */
  readonly install: InstallOutcome | null;
}

/** What the setup found out about a step that did not pass. */
export interface Stopped {
  readonly docker: DockerAdvice | null;
  /** The server has an admin token already; another one needs the user's word. */
  readonly tokenExists: boolean;
  readonly ollama: OllamaMissing | null;
}

export const NOT_STOPPED: Stopped = { docker: null, tokenExists: false, ollama: null };

/**
 * What to do when no Ollama answers on this Mac: stop and ask (the default), install it with
 * Homebrew, or go on with the model in the server's container.
 */
export type OllamaChoice = "ask" | "install" | "skip";

export interface SetupRun {
  /** Ask for another admin token although the server has one. */
  readonly force: boolean;
  readonly ollama: OllamaChoice;
  readonly onProgress: (progress: SetupProgress) => void;
  readonly onStopped: (stopped: Stopped) => void;
  /** Waits between two looks at a download; a test passes one that does not wait. */
  readonly wait?: (milliseconds: number) => Promise<void>;
}

/** How often the download of the model is looked at. */
const POLL_MS = 1000;

const NO_OLLAMA = "Ollama is not on this Mac. Choose below how to go on.";

async function defaultWait(milliseconds: number): Promise<void> {
  await new Promise<void>((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });
}

/** Has Ollama on this Mac download the model, telling the step how far it is meanwhile. */
async function pullWithProgress(backend: Backend, run: SetupRun, model: string): Promise<PullProgress> {
  const wait = run.wait ?? defaultWait;
  let finished = false;
  const pulling = pullHostModel(backend).finally(() => {
    finished = true;
  });
  while (!finished) {
    await wait(POLL_MS);
    if (finished) {
      break;
    }
    const now = await readHostModel(backend);
    if (now.pull !== null) {
      run.onProgress({ at: "model", failure: null, note: describePull(now.pull, model) });
    }
  }
  return pulling;
}

async function downloadModel(backend: Backend, run: SetupRun, model: string): Promise<boolean> {
  run.onProgress({ at: "model", failure: null, note: `Downloading ${model} to Ollama on this Mac…` });
  const pulled = await pullWithProgress(backend, run, model);
  if (pulled.error !== null) {
    run.onProgress({ at: "model", failure: pulled.error });
    return false;
  }
  return true;
}

async function installThenDownload(backend: Backend, run: SetupRun, brew: boolean): Promise<boolean> {
  run.onProgress({ at: "model", failure: null, note: "Installing Ollama with Homebrew…" });
  const installed = await installOllama(backend);
  if (!installed.ok) {
    run.onStopped({ ...NOT_STOPPED, ollama: { brew, install: installed } });
    run.onProgress({ at: "model", failure: installed.error ?? "Homebrew could not install Ollama." });
    return false;
  }
  const host = await readHostModel(backend);
  return host.hasModel ? true : downloadModel(backend, run, host.model);
}

/**
 * Settles where the search model runs before a new server starts. On a Mac it runs in Ollama on
 * the Mac, about a hundred times faster than in Docker there; Rust checks that Ollama answers
 * again when it starts the server, so the choice here is not trusted blindly. Returns whether the
 * setup goes on.
 */
async function prepareModel(backend: Backend, run: SetupRun): Promise<boolean> {
  const host = await readHostModel(backend);
  const plan = modelPlan(host);
  switch (plan) {
    case "container":
    case "ready":
      return true;
    case "download":
      return downloadModel(backend, run, host.model);
    case "missing":
      if (run.ollama === "skip") {
        return true;
      }
      if (run.ollama === "install" && host.brew) {
        return installThenDownload(backend, run, host.brew);
      }
      run.onStopped({ ...NOT_STOPPED, ollama: { brew: host.brew, install: null } });
      run.onProgress({ at: "model", failure: NO_OLLAMA });
      return false;
  }
}

/** Runs the steps in their order. Returns the view with the local connection when all passed. */
export async function runSetup(backend: Backend, run: SetupRun): Promise<ConnectionsView | null> {
  run.onProgress({ at: "docker", failure: null });
  const status = await readLauncherStatus(backend);
  const advice = dockerAdvice(status);
  if (advice !== null) {
    run.onStopped({ ...NOT_STOPPED, docker: advice });
    run.onProgress({ at: "docker", failure: advice.title });
    return null;
  }
  run.onProgress({ at: "model", failure: null });
  // An installed server keeps where its model runs; the Server screen moves it.
  if (!status.installed && !(await prepareModel(backend, run))) {
    return null;
  }
  run.onProgress({ at: "server", failure: null });
  if (!status.running) {
    const started = await changeServer(backend, "start");
    if (!started.ok) {
      run.onProgress({ at: "server", failure: started.error ?? "The launcher could not start the server." });
      return null;
    }
  }
  run.onProgress({ at: "token", failure: null });
  const outcome = await setupToken(backend, run.force);
  if (!outcome.ok) {
    run.onStopped({ ...NOT_STOPPED, tokenExists: tokenExists(outcome) });
    run.onProgress({ at: "token", failure: outcome.error ?? "The server did not create a token." });
    return null;
  }
  run.onProgress({ at: "finished", failure: null });
  return outcome.view;
}
