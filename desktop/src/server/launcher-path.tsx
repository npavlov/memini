import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Button, ErrorNotice, Fact, Facts, Field, Form, Mono, Panel, TextInput } from "../components";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { describeSource } from "./launcher";
import type { LauncherInfo } from "./launcher";
import { setLauncherPath } from "./launcher-api";

export interface LauncherPathProps {
  readonly launcher: LauncherInfo;
  readonly onLauncher: (launcher: LauncherInfo) => void;
}

/**
 * Where the launcher is. The app carries its own; without it, it looks on PATH and where Homebrew
 * and Scoop install. A path set here wins over both and must still name memini-server, the one
 * program the app runs.
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
        <Fact name="It comes from">{describeSource(launcher.source)}</Fact>
      </Facts>
      <Form inline onSubmit={() => saving.run(empty ? null : path)}>
        <Field label="Full path of another memini-server, to use instead" grow>
          <TextInput
            kind="code"
            value={path}
            placeholder="/opt/homebrew/bin/memini-server"
            onChange={setPath}
          />
        </Field>
        <Button submit busy={saving.pending ? "Saving…" : null}>
          {empty ? "Use the app’s own" : "Use this path"}
        </Button>
      </Form>
      <ErrorNotice error={saving.error} />
    </Panel>
  );
}
