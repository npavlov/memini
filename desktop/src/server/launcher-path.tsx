import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Button, ErrorNotice, Fact, Facts, Field, Form, Mono, Panel, TextInput } from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
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
  const empty = path.trim() === "";
  return (
    <Panel title="Launcher program">
      <Facts>
        <Fact name="In use">
          {launcher.resolvedPath === null ? "not found" : <Mono>{launcher.resolvedPath}</Mono>}
        </Fact>
        <Fact name="Found by">
          {launcher.configuredPath === null ? "PATH and the package managers’ folders" : "the path set here"}
        </Fact>
      </Facts>
      <Form inline onSubmit={() => saving.run(empty ? null : path)}>
        <Field label="Full path of memini-server, if it is somewhere else" grow>
          <TextInput
            kind="code"
            value={path}
            placeholder="/opt/homebrew/bin/memini-server"
            onChange={setPath}
          />
        </Field>
        <Button submit disabled={saving.pending}>
          {empty ? "Look on PATH" : "Use this path"}
        </Button>
      </Form>
      <ErrorNotice error={saving.error} />
    </Panel>
  );
}
