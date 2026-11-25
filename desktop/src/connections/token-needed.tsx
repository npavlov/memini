import type { ReactElement } from "react";

import { Actions, Button, Notice, Screen, Text } from "../components";
import type { TokenProblem } from "./connection-check";

export interface TokenNoticeProps {
  readonly problem: TokenProblem;
  /** Leads to where the token is stored: the Connection screen. */
  readonly onAddToken: () => void;
}

/** Says that the connection has no token, or a refused one, with the way to the token. */
export function TokenNotice({ problem, onAddToken }: TokenNoticeProps): ReactElement {
  return (
    <Notice tone={problem.tone} title={problem.label}>
      <Text>{problem.text}</Text>
      <Actions>
        <Button variant="primary" onClick={onAddToken}>
          {problem.action}
        </Button>
      </Actions>
    </Notice>
  );
}

export interface TokenNeededProps extends TokenNoticeProps {
  /** The name of the screen that cannot show its data. */
  readonly title: string;
}

/** What a screen shows in place of its data when the connection's token is missing or refused. */
export function TokenNeeded({ title, problem, onAddToken }: TokenNeededProps): ReactElement {
  return (
    <Screen title={title} lead="This screen shows what the server gives to a token it accepts.">
      <TokenNotice problem={problem} onAddToken={onAddToken} />
    </Screen>
  );
}
