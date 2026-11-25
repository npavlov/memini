import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  EmptyState,
  ErrorNotice,
  Form,
  Mono,
  Panel,
  Stack,
  Tabs,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { callTool, listTools } from "./data-api";
import type { ToolAnswer } from "./data-api";
import { ToolAnswerView } from "./tool-answer";
import { checkValues, toToolCall } from "./tool-call";
import type { FormValues } from "./tool-call";
import { ToolFields } from "./tool-form";
import type { Tool } from "./tool-schema";

export interface ToolConsoleProps {
  readonly connection: Connection;
}

/**
 * The read tools of the server, each with a form built from the arguments the server publishes.
 * A tool the server does not have is not listed. There is no write tool and no query language.
 */
export function ToolConsole({ connection }: ToolConsoleProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(() => listTools(backend, connection.id), [backend, connection.id]),
  );
  return (
    <Panel title="Try a read tool">
      {loaded.state.phase === "loading" ? <Text tone="muted">Reading the server’s tools…</Text> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {loaded.state.phase === "ready" ? (
        <Tools connection={connection} tools={loaded.state.data} />
      ) : null}
    </Panel>
  );
}

interface ToolsProps extends ToolConsoleProps {
  readonly tools: readonly Tool[];
}

function Tools({ connection, tools }: ToolsProps): ReactElement {
  const [chosen, setChosen] = useState(tools[0]?.name ?? "");
  const tool = tools.find((item) => item.name === chosen) ?? tools[0];
  if (tool === undefined) {
    return (
      <EmptyState title="The server publishes none of the read tools">
        <Text>Its OpenAPI document has no route this app knows as a read tool.</Text>
      </EmptyState>
    );
  }
  return (
    <>
      <Tabs
        label="Read tools"
        items={tools.map((item) => ({ id: item.name, label: item.name }))}
        current={tool.name}
        onSelect={setChosen}
      />
      <ToolRunner key={tool.name} connection={connection} tool={tool} />
    </>
  );
}

interface ToolRunnerProps extends ToolConsoleProps {
  readonly tool: Tool;
}

const NO_VALUES: FormValues = new Map();

function ToolRunner({ connection, tool }: ToolRunnerProps): ReactElement {
  const backend = useBackend();
  const [values, setValues] = useState<FormValues>(NO_VALUES);
  const [answer, setAnswer] = useState<ToolAnswer | null>(null);
  const problem = checkValues(tool, values);
  const run = useCallback(async (): Promise<void> => {
    setAnswer(null);
    setAnswer(await callTool(backend, connection.id, toToolCall(tool, values)));
  }, [backend, connection.id, tool, values]);
  const running = useAction(run);
  return (
    <Stack>
      <Text>
        <strong>{tool.summary === "" ? tool.name : tool.summary}</strong>{" "}
        <Text tone="muted" inline>
          <Mono>GET {tool.path}</Mono>
        </Text>
      </Text>
      {tool.description === "" ? null : <Text tone="muted">{tool.description}</Text>}
      <Form onSubmit={() => (problem === null ? running.run() : undefined)}>
        <ToolFields tool={tool} values={values} onChange={setValues} />
        <Actions>
          <Button variant="primary" submit disabled={problem !== null || running.pending}>
            {running.pending ? "Running…" : "Run"}
          </Button>
          {problem === null ? null : (
            <Text tone="hint" inline>
              {problem}
            </Text>
          )}
        </Actions>
      </Form>
      <ErrorNotice error={running.error} />
      {answer === null ? null : <ToolAnswerView answer={answer} />}
    </Stack>
  );
}
