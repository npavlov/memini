import { useState } from "react";
import type { ReactElement } from "react";

import { Button, Notice, Panel, Text } from "../components";
import { displayText } from "../text/display-text";
import { formatInstant } from "../text/format-time";
import type { Project, ProjectPlace } from "./catalog";
import { ProjectEditor } from "./project-editor";
import { ProjectView } from "./project-view";
import { describeSave } from "./save-report";
import type { SaveReport } from "./save-report";

export interface ProjectPanelProps {
  readonly place: ProjectPlace;
  readonly project: Project;
  /** The git sources of the workspace, for the repository of a service. */
  readonly repositories: readonly string[];
  /** Shows the editor at once: for a project that was just created and is still empty. */
  readonly startInEditor: boolean;
  /** Reads the catalog again. */
  readonly onReload: () => void;
}

/** One project: its tree, or its editor. After a save it shows what the server answered. */
export function ProjectPanel(props: ProjectPanelProps): ReactElement {
  const { place, project, onReload } = props;
  const [editing, setEditing] = useState(props.startInEditor);
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
          repositories={props.repositories}
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
