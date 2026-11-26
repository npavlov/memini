import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { Actions, Button, DataTable, ErrorNotice, Mono, Section, Text } from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useBackend } from "../ipc/backend-context";
import { excludeOf, scopeOf } from "./codebases";
import type { Preview, PreviewGroup } from "./codebases";
import type { GitSource } from "./repositories";
import { previewSource, syncSource, updateSource } from "./repositories-api";
import { ScopeChooser, customGlobs } from "./scope-chooser";
import type { Scope } from "./scope-chooser";

export interface ScopeEditorProps {
  readonly connection: Connection;
  readonly workspace: string;
  readonly source: GitSource;
  readonly onSaved: () => void;
}

const NUMBER = new Intl.NumberFormat("en");
/** Groups of the preview shown in each table; the server sums the rest. */
const SHOWN_GROUPS = 12;

function startScope(source: GitSource): Scope {
  const found = scopeOf(source.exclude);
  return { presets: found.presets, custom: found.custom.join("\n") };
}

/**
 * What a code base indexes: the presets and globs it leaves out, a preview of what a sync would
 * keep with them, and saving them, which syncs the code base again with the new scope.
 */
export function ScopeEditor({ connection, workspace, source, onSaved }: ScopeEditorProps): ReactElement {
  const backend = useBackend();
  const [scope, setScope] = useState<Scope>(() => startScope(source));
  const [preview, setPreview] = useState<Preview | null>(null);
  const exclude = excludeOf(scope.presets, customGlobs(scope));
  const look = useCallback(async (): Promise<void> => {
    setPreview(await previewSource(backend, connection.id, workspace, source.name, { exclude }));
  }, [backend, connection.id, exclude, source.name, workspace]);
  const save = useCallback(async (): Promise<void> => {
    await updateSource(backend, connection.id, workspace, source.name, { exclude });
    await syncSource(backend, connection.id, workspace, source.name);
    onSaved();
  }, [backend, connection.id, exclude, onSaved, source.name, workspace]);
  const looking = useAction(look);
  const saving = useAction(save);
  return (
    <Section title="What it indexes">
      <ScopeChooser
        scope={scope}
        onChange={(next) => {
          setScope(next);
          setPreview(null);
        }}
      />
      <Actions>
        <Button disabled={saving.pending} busy={looking.pending ? "Counting…" : null} onClick={() => looking.run()}>
          Preview
        </Button>
        <Button variant="primary" busy={saving.pending ? "Saving…" : null} onClick={() => saving.run()}>
          Save and sync
        </Button>
      </Actions>
      <ErrorNotice error={looking.error ?? saving.error} />
      {preview === null ? (
        <Text tone="muted" small>
          Preview counts the files the server would index with these choices, from its copy of the
          repository. Saving lists the repository again: files left out are taken out of the index.
        </Text>
      ) : (
        <PreviewTables preview={preview} />
      )}
    </Section>
  );
}

function PreviewTables({ preview }: { readonly preview: Preview }): ReactElement {
  return (
    <>
      <Text>
        {NUMBER.format(preview.indexed)} of {NUMBER.format(preview.tracked)} files would be indexed.
      </Text>
      <Groups title="By kind of file" groups={preview.byExtension} />
      <Groups title="By top folder" groups={preview.byFolder} />
    </>
  );
}

function Groups({ title, groups }: { readonly title: string; readonly groups: readonly PreviewGroup[] }): ReactElement {
  return (
    <DataTable columns={[title, "Files", "Indexed"]}>
      {groups.slice(0, SHOWN_GROUPS).map((group) => (
        <tr key={group.key}>
          <th scope="row">
            <Mono>{group.key}</Mono>
          </th>
          <td>{NUMBER.format(group.tracked)}</td>
          <td>{NUMBER.format(group.indexed)}</td>
        </tr>
      ))}
    </DataTable>
  );
}
