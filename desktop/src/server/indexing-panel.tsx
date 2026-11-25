import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, ErrorNotice, Field, Panel, Select, Text } from "../components";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { INDEXING_MODES, MODE_LABELS, describeIndexing, readIndexing, setIndexing } from "./indexing";
import type { IndexingAnswer, IndexingMode } from "./indexing";

function isMode(value: string): value is IndexingMode {
  return INDEXING_MODES.some((mode) => mode === value);
}

/**
 * How many cores the search model of the server on this computer may use while it indexes. A
 * change recreates the model, so an embedding it was doing is tried again.
 */
export function IndexingPanel(): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(useCallback(() => readIndexing(backend), [backend]));
  const [shown, setShown] = useState<IndexingAnswer | null>(null);
  const [chosen, setChosen] = useState<IndexingMode | null>(null);
  const answer = shown ?? (loaded.state.phase === "ready" ? loaded.state.data : null);
  const current = answer?.indexing ?? null;
  const apply = useCallback(
    async (mode: IndexingMode): Promise<void> => {
      setShown(await setIndexing(backend, mode));
      setChosen(null);
    },
    [backend],
  );
  const applying = useAction(apply);
  const selected = chosen ?? current?.mode ?? "half";
  return (
    <Panel title="Indexing">
      <Text>
        How much of this computer the search model may use while it indexes code and messages.
      </Text>
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {answer !== null && !answer.ok && answer.error !== null ? (
        <Text tone="failure">{answer.error}</Text>
      ) : null}
      {current === null ? null : (
        <>
          <Text tone="muted">{describeIndexing(current)}</Text>
          <Field label="Cores for the search model">
            <Select
              value={selected}
              options={INDEXING_MODES.map((mode) => ({ value: mode, label: MODE_LABELS[mode] }))}
              onChange={(value) => {
                if (isMode(value)) {
                  setChosen(value);
                }
              }}
            />
          </Field>
          <ErrorNotice error={applying.error} />
          <Actions>
            <Button
              variant="primary"
              disabled={applying.pending || selected === current.mode}
              onClick={() => applying.run(selected)}
            >
              {applying.pending ? "Applying…" : "Apply"}
            </Button>
            <Text tone="muted" small inline>
              The model restarts; an embedding it was doing is tried again.
            </Text>
          </Actions>
        </>
      )}
    </Panel>
  );
}
