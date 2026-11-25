import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { describeIndexing, parseIndexingAnswer, readIndexing, setIndexing } from "./indexing";

const HALF = { schema: 1, ok: true, error: null, indexing: { mode: "half", cpus: 4, cores: 8 } };

describe("parseIndexingAnswer", () => {
  it("reads the mode and its limit", () => {
    expect(parseIndexingAnswer(HALF)).toEqual({
      ok: true,
      error: null,
      indexing: { mode: "half", cpus: 4, cores: 8 },
    });
  });

  it("reads no limit as null", () => {
    const full = { ...HALF, indexing: { mode: "full", cpus: null, cores: 8 } };

    expect(parseIndexingAnswer(full).indexing?.cpus).toBeNull();
  });

  it("gives the launcher's reason and no state when it refused", () => {
    const refused = parseIndexingAnswer({ schema: 1, ok: false, error: "no managed server at /srv/x" });

    expect(refused).toEqual({ ok: false, error: "no managed server at /srv/x", indexing: null });
  });
});

describe("describeIndexing", () => {
  it.each([
    [{ mode: "half", cpus: 4, cores: 8 } as const, "at most 4 of 8 cores"],
    [{ mode: "background", cpus: 1, cores: 8 } as const, "at most 1 of 8 cores"],
    [{ mode: "full", cpus: null, cores: 8 } as const, "all 8 cores"],
  ])("says what %o leaves to the user", (state, words) => {
    expect(describeIndexing(state)).toContain(words);
  });
});

describe("the indexing commands", () => {
  it("names the mode to Rust and nothing else", async () => {
    const backend = createFakeBackend({
      answers: { launcher_indexing: () => HALF, launcher_indexing_set: () => HALF },
    });

    await readIndexing(backend);
    await setIndexing(backend, "background");

    expect(backend.calls).toEqual([
      { command: "launcher_indexing", args: {} },
      { command: "launcher_indexing_set", args: { mode: "background" } },
    ]);
  });
});
