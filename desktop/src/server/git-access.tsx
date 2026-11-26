import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  Columns,
  ConfirmButton,
  ErrorNotice,
  Fact,
  Facts,
  Field,
  Form,
  Mono,
  Panel,
  Section,
  Select,
  Text,
  TextInput,
} from "../components";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { useBackend } from "../ipc/backend-context";
import { hostsProblem, launcherRefusal, splitHosts } from "../repositories/repositories";
import type { GitAccess, SecretSource } from "../repositories/repositories";
import {
  readGitAccess,
  removeGitAccess,
  setHttpsToken,
  setSshKey,
} from "../repositories/repositories-api";

/**
 * How the server on this computer reaches private remotes: one ssh key with the keys of its hosts,
 * one https token with the hosts it is sent to. The key and the token are read by Rust from the
 * clipboard or a file and never come to this window; only their fingerprints and hosts do.
 */
export function GitAccessPanel(): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(useCallback(() => readGitAccess(backend), [backend]));
  const [shown, setShown] = useState<GitAccess | null>(null);
  const access = shown ?? (loaded.state.phase === "ready" ? loaded.state.data : null);
  return (
    <Panel title="Git access">
      <Text>
        For private repositories at an address. A public repository and a folder of this computer
        need none. Changing it restarts the server for a few seconds.
      </Text>
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {access !== null && !access.ok && access.error !== null ? (
        <Text tone="failure">{access.error}</Text>
      ) : null}
      {access === null ? null : (
        <>
          <SshAccess access={access} onAccess={setShown} />
          <HttpsAccess access={access} onAccess={setShown} />
        </>
      )}
    </Panel>
  );
}

interface AccessProps {
  readonly access: GitAccess;
  readonly onAccess: (access: GitAccess) => void;
}

/** The launcher answered with ok false: its reason is the error. */
function checked(answer: GitAccess): GitAccess {
  if (!answer.ok) {
    throw launcherRefusal(answer.error, "The launcher could not change the git access.");
  }
  return answer;
}

function SshAccess({ access, onAccess }: AccessProps): ReactElement {
  const backend = useBackend();
  const remove = useCallback(async (): Promise<void> => {
    onAccess(checked(await removeGitAccess(backend, "ssh")));
  }, [backend, onAccess]);
  const removing = useAction(remove);
  const ssh = access.ssh;
  if (ssh === null) {
    return (
      <Section title="SSH key">
        <SecretForm
          kind="ssh"
          onAccess={onAccess}
          lead="A private key without a passphrase, best a deploy key with read access made for Memini, not your own key. The host's keys are fetched now: compare their fingerprints with the ones your git server publishes."
        />
      </Section>
    );
  }
  return (
    <Section title="SSH key">
      <Facts>
        <Fact name="Key">
          <Mono>{ssh.key === null ? "unknown" : `${ssh.key.kind} ${ssh.key.fingerprint}`}</Mono>
        </Fact>
        {ssh.hosts.map((host) => (
          <Fact key={host.host} name={host.host}>
            {host.keys.map((key) => (
              <Text key={key.fingerprint} small>
                <Mono>{`${key.kind} ${key.fingerprint}`}</Mono>
              </Text>
            ))}
          </Fact>
        ))}
      </Facts>
      <Text tone="muted" small>
        Compare the host fingerprints with the ones your git server publishes. If they differ, remove
        the key and do not use these hosts.
      </Text>
      <ErrorNotice error={removing.error} />
      <Actions>
        <ConfirmButton
          label="Remove the ssh key"
          question="Remove the server's ssh key and host keys? Sources at ssh addresses stop syncing."
          confirmLabel="Remove the key"
          working="Removing…"
          disabled={removing.pending}
          onConfirm={() => removing.run()}
        />
      </Actions>
    </Section>
  );
}

function HttpsAccess({ access, onAccess }: AccessProps): ReactElement {
  const backend = useBackend();
  const remove = useCallback(async (): Promise<void> => {
    onAccess(checked(await removeGitAccess(backend, "https")));
  }, [backend, onAccess]);
  const removing = useAction(remove);
  const https = access.https;
  if (https === null) {
    return (
      <Section title="HTTPS token">
        <SecretForm
          kind="https"
          onAccess={onAccess}
          lead="A token with read access to the repositories, such as a project or personal access token. It is sent only to the hosts named here."
        />
      </Section>
    );
  }
  return (
    <Section title="HTTPS token">
      <Facts>
        <Fact name="Sent to">{https.hosts.join(", ")}</Fact>
        <Fact name="User name">
          <Mono>{https.username}</Mono>
        </Fact>
      </Facts>
      <ErrorNotice error={removing.error} />
      <Actions>
        <ConfirmButton
          label="Remove the token"
          question="Remove the server's https token? Private sources at https addresses stop syncing."
          confirmLabel="Remove the token"
          working="Removing…"
          disabled={removing.pending}
          onConfirm={() => removing.run()}
        />
      </Actions>
    </Section>
  );
}

interface SecretFormProps {
  readonly kind: "ssh" | "https";
  readonly lead: string;
  readonly onAccess: (access: GitAccess) => void;
}

const SOURCES = [
  { value: "file", label: "From a file" },
  { value: "clipboard", label: "From the clipboard" },
];

function SecretForm({ kind, lead, onAccess }: SecretFormProps): ReactElement {
  const backend = useBackend();
  const [hosts, setHosts] = useState("");
  const [username, setUsername] = useState("");
  const [from, setFrom] = useState("file");
  const [path, setPath] = useState("");
  const problem = hostsProblem(hosts) ?? (from === "file" && path.trim() === "" ? "Enter the path of the file." : null);
  const save = useCallback(async (): Promise<void> => {
    const source: SecretSource = from === "clipboard" ? { from: "clipboard" } : { from: "file", path: path.trim() };
    const answer =
      kind === "ssh"
        ? await setSshKey(backend, splitHosts(hosts), source)
        : await setHttpsToken(backend, splitHosts(hosts), username, source);
    onAccess(checked(answer));
  }, [backend, from, hosts, kind, onAccess, path, username]);
  const saving = useAction(save);
  const what = kind === "ssh" ? "key" : "token";
  return (
    <Form onSubmit={() => (problem === null ? saving.run() : undefined)}>
      <Text small>{lead}</Text>
      <Columns>
        <Field label="Hosts" hint="The git server's host, several separated by commas.">
          <TextInput value={hosts} kind="code" placeholder="git.example.org" onChange={setHosts} />
        </Field>
        {kind === "https" ? (
          <Field label="User name" note="optional" hint="Empty: x-access-token, as GitHub takes it.">
            <TextInput value={username} kind="code" placeholder="oauth2" onChange={setUsername} />
          </Field>
        ) : null}
      </Columns>
      <Columns>
        <Field label={`The ${what}`}>
          <Select value={from} options={SOURCES} onChange={setFrom} />
        </Field>
        {from === "file" ? (
          <Field label="File" grow hint={`Its full path; ~ is your home folder. The ${what} never comes to this window.`}>
            <TextInput
              value={path}
              kind="code"
              placeholder={kind === "ssh" ? "~/.ssh/memini_deploy_key" : "~/memini-git.token"}
              onChange={setPath}
            />
          </Field>
        ) : (
          <Text tone="muted" small>
            Copy the {what}, then save: the app reads it and clears the clipboard.
          </Text>
        )}
      </Columns>
      {hosts === "" || problem === null ? null : <Text tone="hint">{problem}</Text>}
      <ErrorNotice error={saving.error} />
      <Actions>
        <Button variant="primary" submit disabled={problem !== null} busy={saving.pending ? "Saving…" : null}>
          {kind === "ssh" ? "Give the server this key" : "Give the server this token"}
        </Button>
      </Actions>
    </Form>
  );
}
