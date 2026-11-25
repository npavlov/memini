// The first setup as a sequence of commands of the Rust side: read the status, start the server
// if it is not running, then let Rust take the first admin token into the keychain. Each step
// stops the run when it does not pass, and says why.

import type { ConnectionsView } from "../connections/connections";
import type { Backend } from "../ipc/backend";
import { changeServer, readLauncherStatus, setupToken } from "./launcher-api";
import { dockerAdvice, tokenExists } from "./setup";
import type { DockerAdvice, SetupProgress } from "./setup";

/** What the setup found out about a step that did not pass. */
export interface Stopped {
  readonly docker: DockerAdvice | null;
  /** The server has an admin token already; another one needs the user's word. */
  readonly tokenExists: boolean;
}

export const NOT_STOPPED: Stopped = { docker: null, tokenExists: false };

export interface SetupRun {
  /** Ask for another admin token although the server has one. */
  readonly force: boolean;
  readonly onProgress: (progress: SetupProgress) => void;
  readonly onStopped: (stopped: Stopped) => void;
}

/** Runs the steps in their order. Returns the view with the local connection when all passed. */
export async function runSetup(backend: Backend, run: SetupRun): Promise<ConnectionsView | null> {
  run.onProgress({ at: "docker", failure: null });
  const status = await readLauncherStatus(backend);
  const advice = dockerAdvice(status);
  if (advice !== null) {
    run.onStopped({ docker: advice, tokenExists: false });
    run.onProgress({ at: "docker", failure: advice.title });
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
    run.onStopped({ docker: null, tokenExists: tokenExists(outcome) });
    run.onProgress({ at: "token", failure: outcome.error ?? "The server did not create a token." });
    return null;
  }
  run.onProgress({ at: "finished", failure: null });
  return outcome.view;
}
