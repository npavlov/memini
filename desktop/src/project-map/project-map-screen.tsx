import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { DataTable, EmptyState, ErrorNotice, Field, Panel, Screen, Select, Text } from "../components";
import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { displayText } from "../text/display-text";
import { listWorkspaceNames } from "../tokens/tokens-api";
import { readCatalog, repositoriesOf } from "./catalog";
import type { Catalog, ProjectPlace } from "./catalog";
import { NewProjectPanel } from "./new-project";
import { ProjectPanel } from "./project-panel";

export interface ProjectMapScreenProps {
  readonly connection: Connection;
}

/**
 * The catalog of a workspace: its projects, their services, and their domains and subdomains
 * with the code each is responsible for. A project is created here, and edited as a whole and
 * saved against the version it was read at.
 */
export function ProjectMapScreen({ connection }: ProjectMapScreenProps): ReactElement {
  const backend = useBackend();
  const id = connection.id;
  const workspaces = useLoad(useCallback(() => listWorkspaceNames(backend, id), [backend, id]));
  const [chosen, setChosen] = useState("");
  const names = workspaces.state.phase === "ready" ? workspaces.state.data : [];
  const workspace = names.includes(chosen) ? chosen : (names[0] ?? "");
  return (
    <Screen
      title="Project map"
      lead="The projects of a workspace: their services, their domains and subdomains, and the code each is responsible for."
    >
      {workspaces.state.phase === "loading" ? <Text tone="muted">Asking the server…</Text> : null}
      {workspaces.state.phase === "failed" ? <ErrorNotice error={workspaces.state.error} /> : null}
      {workspaces.state.phase === "ready" && workspace === "" ? (
        <EmptyState title="This server has no workspace yet">
          <Text>A project belongs to a workspace. Create one with the server’s command line first.</Text>
        </EmptyState>
      ) : null}
      {workspace === "" ? null : (
        <>
          <Panel title="Workspace">
            <Field label="Workspace">
              <Select
                value={workspace}
                onChange={setChosen}
                options={names.map((name) => ({ value: name, label: name }))}
              />
            </Field>
          </Panel>
          <WorkspaceCatalog key={workspace} place={{ id, workspace }} />
        </>
      )}
    </Screen>
  );
}

interface WorkspaceCatalogProps {
  readonly place: ProjectPlace;
}

/** The projects of one workspace, with the form for a new one. */
function WorkspaceCatalog({ place }: WorkspaceCatalogProps): ReactElement {
  const backend = useBackend();
  const { id, workspace } = place;
  const catalog = useLoad(useCallback(() => readCatalog(backend, { id, workspace }), [backend, id, workspace]));
  // The project that was just created here: it is shown in its editor, because it is empty.
  const [created, setCreated] = useState<string | null>(null);
  const { reload } = catalog;
  const onCreated = useCallback(
    (name: string): void => {
      setCreated(name);
      reload();
    },
    [reload],
  );
  if (catalog.state.phase === "loading") {
    return <Text tone="muted">Reading the catalog…</Text>;
  }
  if (catalog.state.phase === "failed") {
    return <ErrorNotice error={catalog.state.error} />;
  }
  const { projects, unassigned } = catalog.state.data;
  const repositories = repositoriesOf(catalog.state.data);
  return (
    <>
      <NewProjectPanel place={place} onCreated={onCreated} />
      {projects.length === 0 ? (
        <EmptyState title="This workspace has no project yet">
          <Text>
            Create one with New project, or let an agent write it with the put_project tool,
            following catalog_guide. Its services, domains and path masks are edited here.
          </Text>
        </EmptyState>
      ) : null}
      {projects.map((project) => (
        <ProjectPanel
          key={project.name}
          place={place}
          project={project}
          repositories={repositories}
          startInEditor={project.name === created}
          onReload={reload}
        />
      ))}
      <Unassigned repositories={unassigned} />
    </>
  );
}

interface UnassignedProps {
  readonly repositories: Catalog["unassigned"];
}

/** The repositories of the workspace that no service of a project names yet. */
function Unassigned({ repositories }: UnassignedProps): ReactElement | null {
  if (repositories.length === 0) {
    return null;
  }
  return (
    <Panel title="Repositories in no project">
      <DataTable columns={["Repository", "Files"]}>
        {repositories.map((repository) => (
          <tr key={repository.repository}>
            <th scope="row">{displayText(repository.repository)}</th>
            <td>{repository.files === null ? "—" : String(repository.files)}</td>
          </tr>
        ))}
      </DataTable>
    </Panel>
  );
}
