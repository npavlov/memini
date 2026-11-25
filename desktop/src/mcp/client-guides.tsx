import { useMemo, useState } from "react";
import type { ReactElement } from "react";

import { CodeBlock } from "../ui/code-block";
import { Panel } from "../ui/panel";
import { TOKEN_VARIABLE, clientGuides, tokenSetup } from "./mcp-clients";
import type { ClientGuide } from "./mcp-clients";

export interface ClientGuidesProps {
  /** The MCP address, already checked. */
  readonly address: string;
}

/** Configuration to paste into an MCP client. It names the token's variable, never a token. */
export function ClientGuides({ address }: ClientGuidesProps): ReactElement {
  const guides = useMemo(() => clientGuides(address), [address]);
  const [chosen, setChosen] = useState(guides[0]?.id ?? "");
  const guide = guides.find((item) => item.id === chosen) ?? guides[0];
  return (
    <>
      <Panel title="Before you start">
        <p>
          A client needs a token with the read scope. Create one on the Tokens screen, save it to a
          file only you can read, and load it into {TOKEN_VARIABLE} in the shell that starts the
          client. The configuration below reads that variable, so no token is written into a
          project file.
        </p>
        {tokenSetup().map((snippet) => (
          <CodeBlock key={snippet.label} label={snippet.label} text={snippet.text} />
        ))}
      </Panel>
      <Panel title="Client configuration">
        <div className="tabs" role="tablist" aria-label="MCP clients">
          {guides.map((item) => (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={item.id === guide?.id}
              className={item.id === guide?.id ? "tab current" : "tab"}
              onClick={() => {
                setChosen(item.id);
              }}
            >
              {item.name}
            </button>
          ))}
        </div>
        {guide === undefined ? null : <Guide guide={guide} />}
      </Panel>
    </>
  );
}

interface GuideProps {
  readonly guide: ClientGuide;
}

function Guide({ guide }: GuideProps): ReactElement {
  return (
    <div role="tabpanel">
      {guide.snippets.map((snippet) => (
        <CodeBlock key={snippet.label} label={snippet.label} text={snippet.text} />
      ))}
      <p className="muted">{guide.check}</p>
    </div>
  );
}
