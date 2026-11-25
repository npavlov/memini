import { useMemo, useState } from "react";
import type { ReactElement } from "react";

import { CodeBlock, Panel, Stack, Tabs, Text } from "../components";
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
        <Text>
          A client needs a token with the read scope. Create one on the Tokens screen, save it to a
          file only you can read, and load it into {TOKEN_VARIABLE} in the shell that starts the
          client. The configuration below reads that variable, so no token is written into a
          project file.
        </Text>
        {tokenSetup().map((snippet) => (
          <CodeBlock key={snippet.label} label={snippet.label} text={snippet.text} />
        ))}
      </Panel>
      <Panel title="Client configuration">
        <Tabs
          label="MCP clients"
          items={guides.map((item) => ({ id: item.id, label: item.name }))}
          current={guide?.id ?? ""}
          onSelect={setChosen}
        />
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
    <Stack>
      {guide.snippets.map((snippet) => (
        <CodeBlock key={snippet.label} label={snippet.label} text={snippet.text} />
      ))}
      <Text tone="muted">{guide.check}</Text>
    </Stack>
  );
}
