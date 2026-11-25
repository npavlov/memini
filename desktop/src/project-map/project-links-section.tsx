import { useCallback } from "react";
import type { ReactElement } from "react";

import { DataTable, ErrorNotice, Mono, Section, Text } from "../components";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { displayText } from "../text/display-text";
import type { ProjectPlace } from "./catalog";
import { endLabel, readProjectLinks } from "./project-links";
import type { ProjectLink } from "./project-links";

export interface ProjectLinksSectionProps {
  readonly place: ProjectPlace;
  /** The project's name. */
  readonly project: string;
}

/**
 * The links of a project, read-only: what its services use, and which services of other
 * projects use them. A note is what someone wrote into the catalog: it is shown as text.
 */
export function ProjectLinksSection({ place, project }: ProjectLinksSectionProps): ReactElement {
  const backend = useBackend();
  const { id, workspace } = place;
  const links = useLoad(
    useCallback(() => readProjectLinks(backend, { id, workspace }, project), [backend, id, workspace, project]),
  );
  return (
    <Section title="Links between services">
      {links.state.phase === "loading" ? <Text tone="muted">Reading the links…</Text> : null}
      {links.state.phase === "failed" ? <ErrorNotice error={links.state.error} /> : null}
      {links.state.phase === "ready" ? (
        <>
          <LinkTable direction="uses" project={project} links={links.state.data.uses} />
          <LinkTable direction="used_by" project={project} links={links.state.data.usedBy} />
          {links.state.data.uses.length + links.state.data.usedBy.length === 0 ? (
            <Text tone="muted">No service of this project uses another, and none of another project uses one of its.</Text>
          ) : null}
        </>
      ) : null}
    </Section>
  );
}

interface LinkTableProps {
  /** Whether the project's services use the others, or are used by them. */
  readonly direction: "uses" | "used_by";
  readonly project: string;
  readonly links: readonly ProjectLink[];
}

/** The links of one direction. A row starts with the project's own service, whichever end it is. */
function LinkTable({ direction, project, links }: LinkTableProps): ReactElement | null {
  if (links.length === 0) {
    return null;
  }
  const using = (link: ProjectLink): string => endLabel(project, { project: link.fromProject, service: link.fromService });
  const used = (link: ProjectLink): string => endLabel(project, { project: link.toProject, service: link.toService });
  const outgoing = direction === "uses";
  return (
    <DataTable columns={["Service", outgoing ? "Uses" : "Is used by", "How", "Note", "Where it shows"]}>
      {links.map((link, position) => (
        <tr key={String(position)}>
          <th scope="row">{displayText(outgoing ? using(link) : used(link))}</th>
          <td>{displayText(outgoing ? used(link) : using(link))}</td>
          <td>{displayText(link.kind)}</td>
          <td>{link.note === "" ? "—" : displayText(link.note)}</td>
          <td>
            {link.places.length === 0
              ? "—"
              : link.places.map((shown) => (
                  <Text key={shown.path} small>
                    <Mono>{displayText(shown.path)}</Mono>
                    {shown.files === null ? "" : ` · ${countNoun(shown.files, "file")}`}
                  </Text>
                ))}
          </td>
        </tr>
      ))}
    </DataTable>
  );
}
