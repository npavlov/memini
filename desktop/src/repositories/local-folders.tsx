import { useCallback } from "react";
import type { ReactElement } from "react";

import { Button, ConfirmButton, DataTable, ErrorNotice, Mono, Panel, Pill, Text } from "../components";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { launcherRefusal } from "./repositories";
import { listFolders, removeFolder } from "./repositories-api";

export interface LocalFoldersProps {
  readonly onChanged: () => void;
}

const COLUMNS = ["Name", "Folder on this computer", "The server sees it at", "State"];

/** The folders of this computer the local server may read, given by the launcher. */
export function LocalFolders({ onChanged }: LocalFoldersProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(useCallback(() => listFolders(backend), [backend]));
  const { reload } = loaded;
  const remove = useCallback(
    async (name: string): Promise<void> => {
      const answer = await removeFolder(backend, name);
      if (!answer.ok) {
        throw launcherRefusal(answer.error, "The launcher could not take the folder back.");
      }
      reload();
      onChanged();
    },
    [backend, onChanged, reload],
  );
  const removing = useAction(remove);
  const answer = loaded.state.phase === "ready" ? loaded.state.data : null;
  return (
    <Panel
      title="Folders given to the server"
      actions={
        <Button disabled={loaded.busy} onClick={reload}>
          {loaded.busy ? "Refreshing…" : "Refresh"}
        </Button>
      }
    >
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {answer !== null && !answer.ok ? (
        <Text tone="muted">{answer.error ?? "The launcher manages no server on this computer."}</Text>
      ) : null}
      <ErrorNotice error={removing.error} />
      {answer === null || !answer.ok ? null : answer.repositories.length === 0 ? (
        <Text tone="muted">None yet. A folder added above appears here.</Text>
      ) : (
        <DataTable columns={COLUMNS} withActions>
          {answer.repositories.map((repo) => (
            <tr key={repo.name}>
              <th scope="row">{repo.name}</th>
              <td>
                <Mono>{repo.path}</Mono>
              </td>
              <td>
                <Mono>{repo.location}</Mono>
              </td>
              <td>
                {repo.present ? (
                  <Pill tone="good">{repo.branch === null ? "detached HEAD" : `on ${repo.branch}`}</Pill>
                ) : (
                  <Pill tone="bad">folder is gone</Pill>
                )}
              </td>
              <td>
                <ConfirmButton
                  label="Take back"
                  question={`Take “${repo.path}” back from the server? It restarts without the folder, and a source that reads it stops syncing; delete that source above as well. The folder itself is not touched.`}
                  confirmLabel="Take the folder back"
                  disabled={removing.pending}
                  onConfirm={() => removing.run(repo.name)}
                />
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </Panel>
  );
}
