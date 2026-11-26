import { useCallback } from "react";
import type { ReactElement } from "react";

import { ActionOutcome, Button, ConfirmButton, DataTable, ErrorNotice, Mono, Panel, Pill, Text } from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import type { Backend } from "../ipc/backend";
import { useBackend } from "../ipc/backend-context";
import { launcherRefusal, unusedFolders } from "./repositories";
import type { LocalRepo, ReposAnswer } from "./repositories";
import { listFolders, listGitSources, listWorkspaces, removeFolder } from "./repositories-api";

export interface LocalFoldersProps {
  /** The connection to the local server; null when there is none, and every folder is listed. */
  readonly connection: Connection | null;
}

const COLUMNS = ["Name", "Folder on this computer", "The server sees it at", "State"];

interface Folders {
  readonly answer: ReposAnswer;
  /** The folders no code base reads; every folder when the sources cannot be read. */
  readonly unused: readonly LocalRepo[];
  /** How many are read by a code base, and so shown on its card; null when unknown. */
  readonly used: number | null;
}

/** Where every git source of every workspace reads; null when the token cannot list them. */
async function sourceLocations(backend: Backend, connection: Connection | null): Promise<readonly string[] | null> {
  if (connection === null || !connection.hasToken) {
    return null;
  }
  try {
    const names = await listWorkspaces(backend, connection.id);
    const lists = await Promise.all(names.map((name) => listGitSources(backend, connection.id, name)));
    return lists.flat().map((source) => source.location);
  } catch {
    return null;
  }
}

async function readFolders(backend: Backend, connection: Connection | null): Promise<Folders> {
  const [answer, locations] = await Promise.all([listFolders(backend), sourceLocations(backend, connection)]);
  if (locations === null) {
    return { answer, unused: answer.repositories, used: null };
  }
  const unused = unusedFolders(answer.repositories, locations);
  return { answer, unused, used: answer.repositories.length - unused.length };
}

/**
 * The folders of this computer the local server may read, given by the launcher, that no code
 * base uses. A folder a code base reads is the server's, not a workspace's: it is shown on the
 * card of that code base on the Codebases screen.
 */
export function LocalFolders({ connection }: LocalFoldersProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(useCallback(() => readFolders(backend, connection), [backend, connection]));
  const { reload } = loaded;
  const remove = useCallback(
    async (name: string): Promise<string> => {
      const answer = await removeFolder(backend, name);
      if (!answer.ok) {
        throw launcherRefusal(answer.error, "The launcher could not take the folder back.");
      }
      reload();
      return `Taken back: the server restarted without the folder ${name}.`;
    },
    [backend, reload],
  );
  const removing = useAction(remove);
  const found = loaded.state.phase === "ready" ? loaded.state.data : null;
  return (
    <Panel
      title="Folders on this computer"
      actions={
        <Button busy={loaded.busy ? "Refreshing…" : null} onClick={reload}>
          Refresh
        </Button>
      }
    >
      <Text tone="muted">
        Folders given to the server that no code base reads. A folder a code base reads is shown on
        its card on the Codebases screen; delete the code base first to take that folder back.
      </Text>
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {found !== null && !found.answer.ok ? (
        <Text tone="muted">{found.answer.error ?? "The launcher manages no server on this computer."}</Text>
      ) : null}
      {found !== null && found.answer.ok && found.used !== null && found.used > 0 ? (
        <Text tone="muted" small>
          {String(found.used)} {found.used === 1 ? "folder is" : "folders are"} read by a code base.
        </Text>
      ) : null}
      <ActionOutcome of={removing} />
      {found === null || !found.answer.ok ? null : found.unused.length === 0 ? (
        <Text tone="muted">
          {found.answer.repositories.length === 0
            ? "None yet: add a folder on the Codebases screen."
            : "None: every folder given to the server is read by a code base."}
        </Text>
      ) : (
        <DataTable columns={COLUMNS} withActions>
          {found.unused.map((repo) => (
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
                  question={`Take “${repo.path}” back from the server? It restarts without the folder. The folder itself is not touched.`}
                  confirmLabel="Take the folder back"
                  working="Taking it back…"
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
