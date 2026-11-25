// Records marked sensitive (S19): the list of a workspace's marked records and the change of one
// mark, through Rust. The list names records and reasons; it never holds their content.

import type { Backend } from "../ipc/backend";
import { readArray, readObject, readString } from "../json/read-json";
import { displayText } from "../text/display-text";

export interface MarkedRecord {
  readonly recordId: string;
  readonly kind: string;
  readonly source: string;
  readonly externalId: string;
  readonly reason: string | null;
  readonly markedAt: string | null;
}

/** A record id as search results give it: 64 lowercase hexadecimal characters. */
export const RECORD_ID = /^[0-9a-f]{64}$/u;

export function parseMarkedRecords(value: unknown): readonly MarkedRecord[] {
  const answer = readObject(value, "the list of sensitive records");
  return readArray(answer["items"]).map((item) => {
    const record = readObject(item, "a sensitive record");
    const reason = readString(record["reason"]);
    return {
      recordId: readString(record["id"]) ?? "",
      kind: displayText(readString(record["kind"]) ?? ""),
      source: displayText(readString(record["source"]) ?? ""),
      externalId: displayText(readString(record["external_id"]) ?? "", 200),
      reason: reason === null ? null : displayText(reason, 200),
      markedAt: readString(record["marked_at"]),
    };
  });
}

export async function listSensitive(
  backend: Backend,
  id: string,
  workspace: string,
): Promise<readonly MarkedRecord[]> {
  return parseMarkedRecords(await backend.invoke("api_sensitive_list", { id, workspace }));
}

export interface MarkChange {
  readonly workspace: string;
  readonly recordId: string;
  readonly sensitive: boolean;
  readonly reason?: string;
}

export async function changeMark(backend: Backend, id: string, change: MarkChange): Promise<void> {
  await backend.invoke("api_sensitive_mark", {
    id,
    mark: {
      workspace: change.workspace,
      record_id: change.recordId.trim(),
      sensitive: change.sensitive,
      reason: change.reason ?? null,
    },
  });
}
