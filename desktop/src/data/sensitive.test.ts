import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { RECORD_ID, changeMark, listSensitive, parseMarkedRecords } from "./sensitive";

const ID = "a".repeat(64);

describe("parseMarkedRecords", () => {
  it("reads names, reasons and times, and no content", () => {
    const records = parseMarkedRecords({
      workspace: "harbour",
      records: [
        {
          record_id: ID,
          record_kind: "thread",
          source: "mail",
          external_id: "<1@mail.example.test>",
          occurred_at: "2026-11-01T10:00:00Z",
          reason: "salary figures",
          sensitive_at: "2026-11-25T20:00:00Z",
        },
      ],
    });

    expect(records).toEqual([
      {
        recordId: ID,
        kind: "thread",
        source: "mail",
        externalId: "<1@mail.example.test>",
        reason: "salary figures",
        markedAt: "2026-11-25T20:00:00Z",
      },
    ]);
  });
});

describe("the commands", () => {
  it("lists a workspace and marks a record by its id, trimmed", async () => {
    const backend = createFakeBackend({
      answers: {
        api_sensitive_list: () => ({ workspace: "harbour", records: [] }),
        api_sensitive_mark: () => ({ changed: true }),
      },
    });

    expect(await listSensitive(backend, "c1", "harbour")).toEqual([]);
    await changeMark(backend, "c1", {
      workspace: "harbour",
      recordId: ` ${ID} `,
      sensitive: true,
      reason: "salary",
    });

    expect(backend.calls).toEqual([
      { command: "api_sensitive_list", args: { id: "c1", workspace: "harbour" } },
      {
        command: "api_sensitive_mark",
        args: {
          id: "c1",
          mark: { workspace: "harbour", record_id: ID, sensitive: true, reason: "salary" },
        },
      },
    ]);
  });

  it("knows a record id from anything else", () => {
    expect(RECORD_ID.test(ID)).toBe(true);
    expect(RECORD_ID.test(ID.toUpperCase())).toBe(false);
    expect(RECORD_ID.test(ID.slice(1))).toBe(false);
  });
});
