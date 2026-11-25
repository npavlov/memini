import { useCallback } from "react";
import type { ReactElement } from "react";

import { DataTable, Mono, Section, Text, Tree, TreeItem } from "../components";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { countNoun } from "../text/count-noun";
import { displayText } from "../text/display-text";
import { serviceLabel } from "./catalog";
import type { Code, Domain, Project, ProjectPlace, Service, Subdomain } from "./catalog";
import { maskFiles, readDomainFiles } from "./domain-files";
import type { EntryFiles } from "./domain-files";
import { ProjectLinksSection } from "./project-links-section";

const DESCRIPTION_LIMIT = 4000;

export interface ProjectViewProps {
  readonly place: ProjectPlace;
  readonly project: Project;
}

function filesText(files: number | null): string {
  return files === null ? "—" : countNoun(files, "file");
}

/** A project as a tree: its services, its domains with their subdomains and code, and its links. */
export function ProjectView({ place, project }: ProjectViewProps): ReactElement {
  return (
    <>
      {project.description === "" ? null : <Text>{displayText(project.description, DESCRIPTION_LIMIT)}</Text>}
      <Section title="Services">
        {project.services.length === 0 ? (
          <Text tone="muted">The project names no service yet.</Text>
        ) : (
          <DataTable columns={["Service", "Repository", "Directory", "Files"]}>
            {project.services.map((service) => (
              <ServiceLine key={service.name} service={service} />
            ))}
          </DataTable>
        )}
      </Section>
      <Section title="Domains">
        {project.domains.length === 0 ? (
          <Text tone="muted">The project has no domain yet.</Text>
        ) : (
          <Tree label={`Domains of ${displayText(project.name)}`}>
            {project.domains.map((domain) => (
              <DomainNode key={domain.name} place={place} project={project.name} domain={domain} />
            ))}
          </Tree>
        )}
      </Section>
      <ProjectLinksSection place={place} project={project.name} />
    </>
  );
}

interface ServiceLineProps {
  readonly service: Service;
}

function ServiceLine({ service }: ServiceLineProps): ReactElement {
  return (
    <tr>
      <th scope="row">
        {displayText(service.name)}
        {service.description === "" ? null : (
          <Text tone="muted" small>
            {displayText(service.description)}
          </Text>
        )}
      </th>
      <td>{displayText(service.repository)}</td>
      <td>{service.root === "" ? "the whole repository" : <Mono>{displayText(service.root)}</Mono>}</td>
      <td>{service.files === null ? "—" : String(service.files)}</td>
    </tr>
  );
}

interface DomainNodeProps {
  readonly place: ProjectPlace;
  readonly project: string;
  readonly domain: Domain;
}

/** A domain with its subdomains. It asks the server what its masks match now. */
function DomainNode({ place, project, domain }: DomainNodeProps): ReactElement {
  const backend = useBackend();
  const name = domain.name;
  const counted = useLoad(
    useCallback(
      () => readDomainFiles(backend, { ...place, project, domain: name }),
      [backend, place, project, name],
    ),
  );
  const files = counted.state.phase === "ready" ? counted.state.data : null;
  return (
    <EntryNode kind="Domain" project={project} entry={domain} files={files}>
      {domain.subdomains.map((subdomain) => (
        <EntryNode
          key={subdomain.name}
          kind="Subdomain"
          project={project}
          entry={subdomain}
          files={files?.subdomains.get(subdomain.name) ?? null}
        />
      ))}
    </EntryNode>
  );
}

interface EntryNodeProps {
  readonly kind: string;
  readonly project: string;
  readonly entry: Subdomain;
  /** What the entry's masks match; null while it is not known. */
  readonly files: EntryFiles | null;
  /** The subdomains of a domain. */
  readonly children?: ReactElement[];
}

function EntryNode({ kind, project, entry, files, children }: EntryNodeProps): ReactElement {
  const title = entry.title === "" ? "" : ` · ${displayText(entry.title)}`;
  const inside = [
    ...entry.code.map((code) => (
      <CodeNode key={`code:${serviceLabel(code)}`} project={project} code={code} files={files} />
    )),
    ...(children ?? []),
  ];
  return (
    <TreeItem
      kind={kind}
      title={`${displayText(entry.name)}${title}`}
      aside={files === null ? undefined : filesText(files.files)}
      detail={entry.description === "" ? undefined : displayText(entry.description, DESCRIPTION_LIMIT)}
    >
      {inside.length === 0 ? undefined : inside}
    </TreeItem>
  );
}

interface CodeNodeProps {
  readonly project: string;
  readonly code: Code;
  readonly files: EntryFiles | null;
}

/** The code of an entry in one service: its path masks, each with what it matches. */
function CodeNode({ project, code, files }: CodeNodeProps): ReactElement {
  return (
    <TreeItem kind="Code in" title={displayText(serviceLabel(code))}>
      {code.paths.map((path) => {
        const matched = maskFiles(files, { project, code, path });
        return (
          <TreeItem
            key={path}
            kind="Paths"
            title={<Mono>{displayText(path)}</Mono>}
            aside={matched === null ? undefined : filesText(matched)}
          />
        );
      })}
    </TreeItem>
  );
}
