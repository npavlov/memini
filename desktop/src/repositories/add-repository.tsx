import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Columns,
  ErrorNotice,
  Field,
  Form,
  Mono,
  Notice,
  Panel,
  Select,
  Tabs,
  Text,
  TextInput,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import {
  SCHEDULES,
  inputProblem,
  isSshRemote,
  launcherRefusal,
  mayNeedFileSharing,
  readSchedule,
  remoteProblem,
  sourceProblem,
  suggestName,
} from "./repositories";
import { addFolder, addSource } from "./repositories-api";

export interface AddRepositoryProps {
  readonly connection: Connection;
  readonly info: AppInfo;
  readonly workspace: string;
  /** The connection is this computer's server: a folder of this computer can be given to it. */
  readonly local: boolean;
  readonly onAdded: () => void;
}

type Mode = "folder" | "remote";

export function AddRepository(props: AddRepositoryProps): ReactElement {
  const [mode, setMode] = useState<Mode>(props.local ? "folder" : "remote");
  const items = [
    ...(props.local ? [{ id: "folder", label: "A folder on this computer" }] : []),
    { id: "remote", label: "A repository at an address" },
  ];
  return (
    <Panel title={`Add a repository to ${props.workspace}`}>
      <Tabs
        label="Where the repository is"
        items={items}
        current={mode}
        onSelect={(id) => {
          setMode(id === "folder" ? "folder" : "remote");
        }}
      />
      {mode === "folder" && props.local ? <FolderForm {...props} /> : <RemoteForm {...props} />}
    </Panel>
  );
}

interface Common {
  readonly name: string;
  readonly ref: string;
  readonly schedule: string;
}

const START: Common = { name: "", ref: "", schedule: "3600" };

function FolderForm({ connection, info, workspace, onAdded }: AddRepositoryProps): ReactElement {
  const backend = useBackend();
  const [path, setPath] = useState("");
  const [form, setForm] = useState<Common>(START);
  const [warning, setWarning] = useState<string | null>(null);
  const add = useCallback(async (): Promise<void> => {
    setWarning(null);
    const answer = await addFolder(backend, path.trim());
    if (!answer.ok || answer.repository === null) {
      throw launcherRefusal(answer.error, "The launcher could not give the folder to the server.");
    }
    const repo = answer.repository;
    const name = form.name === "" ? suggestName(repo.name) : form.name;
    const ref = form.ref.trim() === "" ? (repo.branch ?? "main") : form.ref.trim();
    const problem = sourceProblem(name, ref);
    if (problem !== null) {
      throw inputProblem(problem);
    }
    await addSource(backend, connection.id, {
      workspace,
      name,
      location: repo.location,
      ref,
      scheduleSeconds: readSchedule(form.schedule),
    });
    setWarning(answer.warning);
    setPath("");
    setForm(START);
    onAdded();
  }, [backend, connection.id, form, onAdded, path, workspace]);
  const adding = useAction(add);
  const named = form.name === "" || sourceProblem(form.name, "main") === null;
  const ready = path.trim().startsWith("/") || /^[A-Za-z]:[\\/]/u.test(path.trim());
  return (
    <Form onSubmit={() => (ready && named ? adding.run() : undefined)}>
      <Text>
        The server reads the folder’s git history, read-only, and follows the branch you name. It sees
        new commits once you commit or pull in this folder. The app gives the folder to the server and
        restarts it, which takes a few seconds.
      </Text>
      <Field label="Folder of the repository" hint="Its full path, the folder that holds .git.">
        <TextInput value={path} kind="code" placeholder="/Users/sam/src/harbour" onChange={setPath} />
      </Field>
      {mayNeedFileSharing(info.os, path) && path.trim() !== "" ? (
        <Notice tone="warn" title="Docker Desktop may not show this folder to the server">
          <Text>
            Docker Desktop shares only some folders with containers, /Users among them. For a folder
            elsewhere, such as a disk under <Mono>/Volumes</Mono>, check that it, or a folder above it,
            is listed in Docker Desktop → Settings → Resources → File sharing. The app checks the
            folder after it is added and says so if the server cannot see it.
          </Text>
        </Notice>
      ) : null}
      <SourceFields form={form} onChange={setForm} refHint="Empty: the branch checked out in the folder." />
      {named ? null : <Text tone="hint">{sourceProblem(form.name, "main")}</Text>}
      {warning === null ? null : <Notice tone="info" title={warning} />}
      <ErrorNotice error={adding.error} />
      <Actions>
        <Button variant="primary" submit disabled={!ready || !named || adding.pending}>
          {adding.pending ? "Adding…" : "Add the folder"}
        </Button>
      </Actions>
    </Form>
  );
}

function RemoteForm({ connection, workspace, local, onAdded }: AddRepositoryProps): ReactElement {
  const backend = useBackend();
  const [location, setLocation] = useState("");
  const [form, setForm] = useState<Common>({ ...START, ref: "main" });
  const name = form.name === "" ? suggestName(location) : form.name;
  const problem = remoteProblem({ location, name, ref: form.ref });
  const add = useCallback(async (): Promise<void> => {
    await addSource(backend, connection.id, {
      workspace,
      name,
      location,
      ref: form.ref,
      scheduleSeconds: readSchedule(form.schedule),
    });
    setLocation("");
    setForm({ ...START, ref: "main" });
    onAdded();
  }, [backend, connection.id, form, location, name, onAdded, workspace]);
  const adding = useAction(add);
  return (
    <Form onSubmit={() => (problem === null ? adding.run() : undefined)}>
      <Field
        label="Address"
        hint="An https:// address, an ssh:// one, or git@host:path. A private one needs the server's git access."
      >
        <TextInput
          value={location}
          kind="code"
          placeholder="https://git.example.org/harbour/app.git"
          onChange={setLocation}
        />
      </Field>
      {isSshRemote(location) ? (
        <Notice tone="info" title="An ssh address needs the server's ssh key">
          <Text>
            {local
              ? "Give the server a key on the Server screen, under Git access, with this host. Then add the repository here."
              : "The server's operator sets its ssh key and the host's key."}
          </Text>
        </Notice>
      ) : null}
      <SourceFields
        form={{ ...form, name: form.name }}
        namePlaceholder={location === "" ? "app" : suggestName(location)}
        onChange={setForm}
        refHint="The branch to follow."
      />
      {location === "" || problem === null ? null : <Text tone="hint">{problem}</Text>}
      <ErrorNotice error={adding.error} />
      <Actions>
        <Button variant="primary" submit disabled={problem !== null || adding.pending}>
          {adding.pending ? "Adding…" : "Add the repository"}
        </Button>
      </Actions>
    </Form>
  );
}

interface SourceFieldsProps {
  readonly form: Common;
  readonly onChange: (form: Common) => void;
  readonly refHint: string;
  readonly namePlaceholder?: string;
}

function SourceFields({ form, onChange, refHint, namePlaceholder }: SourceFieldsProps): ReactElement {
  return (
    <Columns>
      <Field label="Name" note="optional" hint="How tools name it. Empty: from the repository's name.">
        <TextInput
          value={form.name}
          kind="code"
          maxLength={63}
          placeholder={namePlaceholder ?? "from the folder's name"}
          onChange={(name) => {
            onChange({ ...form, name });
          }}
        />
      </Field>
      <Field label="Branch" hint={refHint}>
        <TextInput
          value={form.ref}
          kind="code"
          placeholder="main"
          onChange={(ref) => {
            onChange({ ...form, ref });
          }}
        />
      </Field>
      <Field label="Sync">
        <Select
          value={form.schedule}
          options={SCHEDULES}
          onChange={(schedule) => {
            onChange({ ...form, schedule });
          }}
        />
      </Field>
    </Columns>
  );
}
