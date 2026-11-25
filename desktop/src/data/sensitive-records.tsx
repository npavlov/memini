import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import {
  Actions,
  Button,
  ConfirmButton,
  DataTable,
  ErrorNotice,
  Field,
  Form,
  Mono,
  NoWrap,
  Notice,
  Section,
  Text,
  TextInput,
} from "../components";
import type { Connection } from "../connections/connections";
import { useAction } from "../hooks/use-action";
import { useLoad } from "../hooks/use-load";
import { AppError } from "../ipc/app-error";
import { useBackend } from "../ipc/backend-context";
import { formatInstant } from "../text/format-time";
import { RECORD_ID, changeMark, listSensitive } from "./sensitive";
import type { MarkedRecord } from "./sensitive";

export interface SensitiveRecordsProps {
  readonly connection: Connection;
  readonly workspace: string;
}

const COLUMNS = ["Record", "Kind", "Source", "Marked", "Reason", ""];

/**
 * The records of a workspace marked sensitive (S19): kept and indexed, left out of answers unless
 * a reader asks with the sensitive scope. Names and reasons only; listing and marking need an
 * admin token, and without one the section says so instead of failing.
 */
export function SensitiveRecords({ connection, workspace }: SensitiveRecordsProps): ReactElement {
  const backend = useBackend();
  const loaded = useLoad(
    useCallback(
      () => listSensitive(backend, connection.id, workspace),
      [backend, connection.id, workspace],
    ),
  );
  const [adding, setAdding] = useState(false);
  const { reload } = loaded;
  const unmark = useAction(
    useCallback(
      async (recordId: string): Promise<void> => {
        await changeMark(backend, connection.id, { workspace, recordId, sensitive: false });
        reload();
      },
      [backend, connection.id, workspace, reload],
    ),
  );
  if (loaded.state.phase === "failed" && isNotAdmin(loaded.state.error)) {
    return (
      <Text tone="muted" small>
        Records marked sensitive are listed and marked with an admin token.
      </Text>
    );
  }
  return (
    <Section
      title="Marked sensitive"
      aside={
        adding ? null : (
          <Button onClick={() => setAdding(true)}>Mark a record…</Button>
        )
      }
    >
      <Text tone="muted" small>
        Kept and indexed, but left out of answers unless a reader asks for them with a token
        that has the sensitive scope. No model reads them.
      </Text>
      {adding ? (
        <MarkForm
          connection={connection}
          workspace={workspace}
          onDone={() => {
            setAdding(false);
            reload();
          }}
          onCancel={() => setAdding(false)}
        />
      ) : null}
      {loaded.state.phase === "loading" ? <Text tone="muted">Asking the server…</Text> : null}
      {loaded.state.phase === "failed" ? <ErrorNotice error={loaded.state.error} /> : null}
      {loaded.state.phase === "ready" ? (
        <MarkedTable records={loaded.state.data} busy={unmark.pending} onUnmark={unmark.run} />
      ) : null}
      <ErrorNotice error={unmark.error} />
    </Section>
  );
}

function isNotAdmin(error: AppError): boolean {
  return error.status === 403;
}

interface MarkedTableProps {
  readonly records: readonly MarkedRecord[];
  readonly busy: boolean;
  readonly onUnmark: (recordId: string) => Promise<void>;
}

function MarkedTable({ records, busy, onUnmark }: MarkedTableProps): ReactElement {
  if (records.length === 0) {
    return <Text tone="muted">No record of this workspace is marked sensitive.</Text>;
  }
  return (
    <DataTable columns={COLUMNS}>
      {records.map((record) => (
        <tr key={record.recordId}>
          <th scope="row">
            <Mono>{record.externalId}</Mono>
          </th>
          <td>{record.kind}</td>
          <td>{record.source}</td>
          <td>
            <NoWrap>{formatInstant(record.markedAt)}</NoWrap>
          </td>
          <td>{record.reason ?? "—"}</td>
          <td>
            <ConfirmButton
              label="Unmark"
              question="Answers will include this record again, and models may read it."
              confirmLabel="Unmark it"
              disabled={busy}
              onConfirm={() => onUnmark(record.recordId)}
            />
          </td>
        </tr>
      ))}
    </DataTable>
  );
}

interface MarkFormProps {
  readonly connection: Connection;
  readonly workspace: string;
  readonly onDone: () => void;
  readonly onCancel: () => void;
}

function MarkForm({ connection, workspace, onDone, onCancel }: MarkFormProps): ReactElement {
  const backend = useBackend();
  const [recordId, setRecordId] = useState("");
  const [reason, setReason] = useState("");
  const valid = RECORD_ID.test(recordId.trim());
  const mark = useAction(
    useCallback(async (): Promise<void> => {
      await changeMark(backend, connection.id, {
        workspace,
        recordId,
        sensitive: true,
        reason,
      });
      onDone();
    }, [backend, connection.id, workspace, recordId, reason, onDone]),
  );
  return (
    <Form onSubmit={() => (valid ? mark.run() : undefined)}>
      <Field
        label="Record id"
        hint="64 hexadecimal characters: the record_id of a search hit or a thread."
      >
        <TextInput value={recordId} onChange={setRecordId} kind="code" maxLength={64} />
      </Field>
      <Field label="Reason" note="optional" hint="Shown here only, at most 200 characters.">
        <TextInput value={reason} onChange={setReason} maxLength={200} />
      </Field>
      {recordId.trim() !== "" && !valid ? (
        <Notice tone="warn" title="That is not a record id" />
      ) : null}
      <ErrorNotice error={mark.error} />
      <Actions>
        <Button variant="primary" submit disabled={!valid || mark.pending}>
          {mark.pending ? "Marking…" : "Mark sensitive"}
        </Button>
        <Button onClick={onCancel}>Cancel</Button>
      </Actions>
    </Form>
  );
}
