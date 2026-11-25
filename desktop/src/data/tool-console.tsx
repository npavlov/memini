import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import type { Connection } from "../connections/connections";
import { useBackend } from "../ipc/backend-context";
import { ErrorNotice, Notice } from "../ui/notice";
import { Panel } from "../ui/panel";
import { useAction } from "../ui/use-action";
import { useLoad } from "../ui/use-load";
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
      {loaded.state.phase === "loading" ? <p className="muted">Reading the server’s tools…</p> : null}
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
      <Notice tone="warn" title="The server publishes none of the read tools">
        <p>Its OpenAPI document has no route this app knows as a read tool.</p>
      </Notice>
    );
  }
  return (
    <>
      <div className="tabs" role="tablist" aria-label="Read tools">
        {tools.map((item) => (
          <button
            key={item.name}
            type="button"
            role="tab"
            aria-selected={item.name === tool.name}
            className={item.name === tool.name ? "tab current" : "tab"}
            onClick={() => {
              setChosen(item.name);
            }}
          >
            {item.name}
          </button>
        ))}
      </div>
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
    <div role="tabpanel" className="tool">
      <p>
        <strong>{tool.summary === "" ? tool.name : tool.summary}</strong>{" "}
        <code className="muted">GET {tool.path}</code>
      </p>
      {tool.description === "" ? null : <p className="muted">{tool.description}</p>}
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          return problem === null ? running.run() : undefined;
        }}
      >
        <ToolFields tool={tool} values={values} onChange={setValues} />
        <div className="actions">
          <button type="submit" className="button primary" disabled={problem !== null || running.pending}>
            {running.pending ? "Running…" : "Run"}
          </button>
          {problem === null ? null : <span className="hint">{problem}</span>}
        </div>
      </form>
      <ErrorNotice error={running.error} />
      {answer === null ? null : <ToolAnswerView answer={answer} />}
    </div>
  );
}
