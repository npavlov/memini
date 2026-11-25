import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import { describePull, installOllama, modelPlan, parseHostModel, pullHostModel, readHostModel } from "./host-model";
import type { HostModel, PullProgress } from "./host-model";

const MAC = {
  supported: true,
  found: true,
  version: "0.34.2",
  model: "qwen3-embedding:0.6b",
  has_model: true,
  brew: true,
  pull: null,
  installing: false,
  install: null,
};

function hostModel(changes: Partial<HostModel>): HostModel {
  return { ...parseHostModel(MAC), ...changes };
}

function pull(changes: Partial<PullProgress>): PullProgress {
  return { status: "pulling", completed: 0, total: 0, done: false, error: null, ...changes };
}

describe("parseHostModel", () => {
  it("reads what Rust says of Ollama on this computer", () => {
    expect(parseHostModel(MAC)).toEqual({
      supported: true,
      found: true,
      version: "0.34.2",
      model: "qwen3-embedding:0.6b",
      hasModel: true,
      brew: true,
      pull: null,
      installing: false,
      install: null,
    });
  });

  it("reads a download and an installation in progress", () => {
    const busy = parseHostModel({
      ...MAC,
      pull: { status: "pulling", completed: 5, total: 10, done: false, error: null },
      install: { ok: false, error: "brew install ollama failed.", tail: ["Error: no network"] },
    });

    expect(busy.pull).toEqual(pull({ completed: 5, total: 10 }));
    expect(busy.install).toEqual({ ok: false, error: "brew install ollama failed.", tail: ["Error: no network"] });
  });
});

describe("modelPlan", () => {
  it.each([
    ["not a Mac", { supported: false }, "container"],
    ["Ollama on the Mac has the model", {}, "ready"],
    ["Ollama on the Mac lacks the model", { hasModel: false }, "download"],
    ["no Ollama answers on the Mac", { found: false, hasModel: false }, "missing"],
  ] as const)("chooses what to do when %s", (_what, changes, plan) => {
    expect(modelPlan(hostModel(changes))).toBe(plan);
  });
});

describe("describePull", () => {
  it.each([
    [pull({ completed: 319_500_000, total: 639_000_000 }), "50%, 320 of 639 MB"],
    [pull({}), "Downloading qwen3-embedding:0.6b to Ollama on this Mac…"],
    [pull({ done: true }), "qwen3-embedding:0.6b is on this Mac."],
    [pull({ done: true, error: "no space left" }), "no space left"],
  ])("says how far %o is", (progress, words) => {
    expect(describePull(progress, "qwen3-embedding:0.6b")).toContain(words);
  });
});

describe("the commands for Ollama on this computer", () => {
  it("names each command to Rust with no argument from the window", async () => {
    const backend = createFakeBackend({
      answers: {
        host_model_status: () => MAC,
        host_model_pull: () => pull({ status: "success", done: true }),
        host_ollama_install: () => ({ ok: true, error: null, tail: [] }),
      },
    });

    await readHostModel(backend);
    await pullHostModel(backend);
    await installOllama(backend);

    expect(backend.calls).toEqual([
      { command: "host_model_status", args: {} },
      { command: "host_model_pull", args: {} },
      { command: "host_ollama_install", args: {} },
    ]);
  });
});
