import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { useBackend } from "../ipc/backend-context";
import { ErrorNotice } from "../ui/notice";
import { Fact, Panel } from "../ui/panel";
import { useAction } from "../ui/use-action";
import type { LauncherInfo } from "./launcher";
import { setLauncherPath } from "./launcher-api";

export interface LauncherPathProps {
  readonly launcher: LauncherInfo;
  readonly onLauncher: (launcher: LauncherInfo) => void;
}

/**
 * Where the launcher is. The app looks on PATH and where Homebrew and Scoop install; a path set
 * here must still name memini-server, the one program the app runs.
 */
export function LauncherPath({ launcher, onLauncher }: LauncherPathProps): ReactElement {
  const backend = useBackend();
  const [path, setPath] = useState(launcher.configuredPath ?? "");
  const save = useCallback(
    async (next: string | null): Promise<void> => {
      const info = await setLauncherPath(backend, next);
      onLauncher(info);
      setPath(info.configuredPath ?? "");
    },
    [backend, onLauncher],
  );
  const saving = useAction(save);
  return (
    <Panel title="Launcher program">
      <dl className="facts">
        <Fact name="In use">{launcher.resolvedPath ?? "not found"}</Fact>
        <Fact name="Found by">
          {launcher.configuredPath === null ? "PATH and the package managers’ folders" : "the path set here"}
        </Fact>
      </dl>
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          return saving.run(path.trim() === "" ? null : path);
        }}
      >
        <label className="field grow">
          <span>Full path of memini-server, if it is somewhere else</span>
          <input
            type="text"
            spellCheck={false}
            autoCapitalize="off"
            autoCorrect="off"
            value={path}
            placeholder="/opt/homebrew/bin/memini-server"
            onChange={(event) => {
              setPath(event.target.value);
            }}
          />
        </label>
        <button type="submit" className="button" disabled={saving.pending}>
          {path.trim() === "" ? "Look on PATH" : "Use this path"}
        </button>
      </form>
      <ErrorNotice error={saving.error} />
    </Panel>
  );
}
