import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { ErrorNotice, Screen, Text } from "../components";
import type { ConnectionsView } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import type { LauncherInfo } from "./launcher";
import { readLauncherInfo } from "./launcher-api";
import { LauncherMissing } from "./launcher-missing";
import { LauncherPath } from "./launcher-path";
import { LocalServer } from "./local-server";

export interface ServerScreenProps {
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
}

/** The server on this computer, through the `memini-server` launcher. */
export function ServerScreen({ view, info, onView }: ServerScreenProps): ReactElement {
  const backend = useBackend();
  const located = useLoad(useCallback(() => readLauncherInfo(backend), [backend]));
  const [changed, setChanged] = useState<LauncherInfo | null>(null);
  return (
    <Screen
      title="Server"
      lead="The Memini server on this computer: Docker, the installation, and each service."
    >
      {located.state.phase === "loading" ? <Text tone="muted">Looking for the launcher…</Text> : null}
      {located.state.phase === "failed" ? <ErrorNotice error={located.state.error} /> : null}
      {located.state.phase === "ready" ? (
        <Launcher
          launcher={changed ?? located.state.data}
          view={view}
          info={info}
          onView={onView}
          onLauncher={setChanged}
        />
      ) : null}
    </Screen>
  );
}

interface LauncherProps extends ServerScreenProps {
  readonly launcher: LauncherInfo;
  readonly onLauncher: (launcher: LauncherInfo) => void;
}

function Launcher({ launcher, view, info, onView, onLauncher }: LauncherProps): ReactElement {
  return (
    <>
      {launcher.resolvedPath === null ? (
        <LauncherMissing os={info.os} configuredPath={launcher.configuredPath} />
      ) : (
        <LocalServer key={launcher.resolvedPath} view={view} info={info} onView={onView} />
      )}
      <LauncherPath launcher={launcher} onLauncher={onLauncher} />
    </>
  );
}
