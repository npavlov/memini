import { describe, expect, it } from "vitest";

import { AppError } from "../ipc/app-error";
import { runOnce } from "./use-action";
import type { RunReport } from "./use-action";

interface Log {
  readonly events: string[];
  readonly report: RunReport<string>;
}

function log(): Log {
  const events: string[] = [];
  return {
    events,
    report: {
      started: (argument) => events.push(`started ${argument}`),
      succeeded: (done) => events.push(`done ${done ?? "-"}`),
      failed: (error) => events.push(`failed ${error.message}`),
      settled: () => events.push("settled"),
    },
  };
}

describe("an action", () => {
  it("reports that it runs, what it did, and that it settled", async () => {
    const { events, report } = log();

    await runOnce({ current: false }, async (what: string) => `${what}: no changes`, "sync", report);

    expect(events).toEqual(["started sync", "done sync: no changes", "settled"]);
  });

  it("says nothing it did when the result shows on the screen", async () => {
    const { events, report } = log();

    await runOnce({ current: false }, async () => undefined, "save", report);

    expect(events).toContain("done -");
  });

  it("never runs a second time while the first runs", async () => {
    const { events, report } = log();
    const gate = { current: false };
    let calls = 0;
    let finish = (): void => undefined;
    const slow = (): Promise<string> =>
      new Promise((resolve) => {
        calls += 1;
        finish = () => {
          resolve("done");
        };
      });

    const first = runOnce(gate, slow, "sync", report);
    await runOnce(gate, slow, "sync", report);
    finish();
    await first;

    expect(calls).toBe(1);
    expect(events).toEqual(["started sync", "done done", "settled"]);
  });

  it("can run again once it settled, also after a failure", async () => {
    const { events, report } = log();
    const gate = { current: false };

    await runOnce(gate, async () => Promise.reject(new AppError({ kind: "invalid", message: "No" })), "a", report);
    await runOnce(gate, async () => "Yes", "b", report);

    expect(events).toEqual(["started a", "failed No", "settled", "started b", "done Yes", "settled"]);
  });
});
