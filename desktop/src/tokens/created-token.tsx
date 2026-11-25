import type { ReactElement } from "react";

import { Actions, Button, CodeBlock, CopyButton, Notice, Panel, Text } from "../components";
import type { CreatedToken } from "./tokens";

export interface CreatedTokenPanelProps {
  readonly created: CreatedToken;
  /** Called when the user has saved the token: the screen then forgets it. */
  readonly onDone: () => void;
}

/**
 * The one place a token is shown: right after its creation, because the server keeps only its
 * digest and cannot show it again. It lives in this component's props until the user closes it,
 * and is copied as a secret.
 */
export function CreatedTokenPanel({ created, onDone }: CreatedTokenPanelProps): ReactElement {
  return (
    <Panel title={`New token: ${created.info.name}`}>
      <Notice tone="warn" title="Copy this token now. It is shown this once.">
        <Text>
          The server stores only a digest of it. Save it where its user will read it from, such as
          a file only they can read; do not paste it into a chat or a project file.
        </Text>
      </Notice>
      <CodeBlock text={created.secret} secret />
      <Actions>
        <CopyButton text={created.secret} label="Copy the token" secret />
        <Button variant="primary" onClick={onDone}>
          I have saved it
        </Button>
      </Actions>
    </Panel>
  );
}
