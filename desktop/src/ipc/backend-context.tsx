import { createContext, useContext } from "react";
import type { ReactElement, ReactNode } from "react";

import type { Backend } from "./backend";

/** A component asked for the backend outside a BackendProvider: a mistake in the app's assembly. */
export class MissingBackendError extends Error {
  constructor() {
    super("No backend was provided to this part of the app.");
    this.name = "MissingBackendError";
  }
}

const BackendContext = createContext<Backend | null>(null);

export interface BackendProviderProps {
  readonly backend: Backend;
  readonly children: ReactNode;
}

export function BackendProvider({ backend, children }: BackendProviderProps): ReactElement {
  return <BackendContext.Provider value={backend}>{children}</BackendContext.Provider>;
}

export function useBackend(): Backend {
  const backend = useContext(BackendContext);
  if (backend === null) {
    throw new MissingBackendError();
  }
  return backend;
}
