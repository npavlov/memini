import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Button,
  DataTable,
  EmptyState,
  ErrorNotice,
  Field,
  Notice,
  Panel,
  Screen,
  Select,
  Text,
} from "../components";
import type { Connection } from "../connections/connections";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { displayText } from "../text/display-text";
import { formatInstant } from "../text/format-time";
import { listWorkspaceNames } from "../tokens/tokens-api";
import { readCatalog } from "./catalog";
import type { Catalog, Project, ProjectPlace } from "./catalog";
import { ProjectEditor } from "./project-editor";
import { ProjectView } from "./project-view";
import { describeSave } from "./save-report";
import type { SaveReport } from "./save-report";

export interface ProjectMapScreenProps {
  readonly connection: Connection;
}

/**
 * The catalog of a workspace: its projects, their services, and their domains and subdomains
 * with the code each is responsible for. A project is edited as a whole and saved against the
 * version it was read at.
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

function WorkspaceCatalog({ place }: WorkspaceCatalogProps): ReactElement {
  const backend = useBackend();
  const { id, workspace } = place;
  const catalog = useLoad(useCallback(() => readCatalog(backend, { id, workspace }), [backend, id, workspace]));
  if (catalog.state.phase === "loading") {
    return <Text tone="muted">Reading the catalog…</Text>;
  }
  if (catalog.state.phase === "failed") {
    return <ErrorNotice error={catalog.state.error} />;
  }
  const { projects, unassigned } = catalog.state.data;
  return (
    <>
      {projects.length === 0 ? (
        <EmptyState title="This workspace has no project yet">
          <Text>
            An agent writes the first project with the put_project tool, following catalog_guide.
            After that its titles, descriptions, domains and path masks can be edited here.
          </Text>
        </EmptyState>
      ) : null}
      {projects.map((project) => (
        <ProjectPanel key={project.name} place={place} project={project} onReload={catalog.reload} />
      ))}
      <Unassigned repositories={unassigned} />
    </>
  );
}

interface ProjectPanelProps {
  readonly place: ProjectPlace;
  readonly project: Project;
  readonly onReload: () => void;
}

/** One project: its tree, or its editor. After a save it shows what the server answered. */
function ProjectPanel({ place, project, onReload }: ProjectPanelProps): ReactElement {
  const [editing, setEditing] = useState(false);
  const [report, setReport] = useState<SaveReport | null>(null);
  const close = (saved: SaveReport | null): void => {
    setReport(saved);
    setEditing(false);
    onReload();
  };
  const title = project.title === "" ? project.name : `${project.title} (${project.name})`;
  return (
    <Panel
      title={displayText(title)}
      actions={editing ? undefined : <Button onClick={() => setEditing(true)}>Edit</Button>}
    >
      <Text tone="muted" small>
        Version {String(project.version)}, last changed {formatInstant(project.updatedAt)}.
      </Text>
      {report === null || editing ? null : <SaveNotice report={report} />}
      {editing ? (
        <ProjectEditor
          place={place}
          project={project}
          onSaved={close}
          onCancel={() => setEditing(false)}
          onReload={() => close(null)}
        />
      ) : (
        <ProjectView place={place} project={project} />
      )}
    </Panel>
  );
}

interface SaveNoticeProps {
  readonly report: SaveReport;
}

function SaveNotice({ report }: SaveNoticeProps): ReactElement {
  const summary = describeSave(report);
  return (
    <Notice tone={summary.tone} title={summary.title}>
      {summary.lines.map((line) => (
        <Text key={line}>{displayText(line)}</Text>
      ))}
    </Notice>
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
