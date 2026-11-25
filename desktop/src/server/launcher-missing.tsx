import type { ReactElement } from "react";

import { CodeBlock, Notice, Panel, Text } from "../components";
import { installSteps } from "./launcher";

export interface LauncherMissingProps {
  readonly os: string;
  /** A path the user set that has no file; null when the program was looked for on PATH. */
  readonly configuredPath: string | null;
}

/** The launcher is not installed. That is a normal state: say what it is and how to get it. */
export function LauncherMissing({ os, configuredPath }: LauncherMissingProps): ReactElement {
  return (
    <Panel title="The launcher is not installed">
      <Notice tone="info" title="memini-server was not found on this computer">
        <Text>
          {configuredPath === null
            ? "This app does not contain the server. It runs the memini-server launcher, which starts Memini with Docker."
            : "There is no memini-server at the path set below. Correct the path, or clear it to look on PATH."}
        </Text>
      </Notice>
      <Text>
        Start Docker with Compose 2.24 or later, then install the launcher in a terminal. Allow 4
        CPU cores, 8 GB of memory and 20 GB of free disk.
      </Text>
      {installSteps(os).map((step) => (
        <CodeBlock key={step.title} label={step.title} text={step.text} />
      ))}
      <Text tone="muted">
        Then open this screen again. To connect to a server that runs elsewhere, you do not need the
        launcher: add it on the Connection screen.
      </Text>
    </Panel>
  );
}
