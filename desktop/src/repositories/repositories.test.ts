import { describe, expect, it } from "vitest";

import { createFakeBackend } from "../ipc/fake-backend";
import {
  hostOf,
  hostsProblem,
  isSshRemote,
  mayNeedFileSharing,
  parseGitAccess,
  parseGitSources,
  parseRefStates,
  parseReposAnswer,
  readSchedule,
  remoteProblem,
  splitHosts,
  suggestName,
} from "./repositories";
import { addFolder, addSource, setHttpsToken, setSshKey } from "./repositories-api";

const REPO = {
  name: "harbour-app",
  path: "/Users/sam/src/harbour-app",
  location: "/srv/local/harbour-app",
  branch: "trunk",
  present: true,
};

describe("parseReposAnswer", () => {
  it("reads the folder that was added and the list", () => {
    const answer = parseReposAnswer({
      schema: 1,
      ok: true,
      error: null,
      repository: REPO,
      warning: null,
      repositories: [REPO, { ...REPO, name: "gone", present: false, branch: null }],
    });

    expect(answer.repository?.location).toBe("/srv/local/harbour-app");
    expect(answer.repositories.map((repo) => [repo.name, repo.present, repo.branch])).toEqual([
      ["harbour-app", true, "trunk"],
      ["gone", false, null],
    ]);
  });

  it("reads a refusal, which has no list", () => {
    const answer = parseReposAnswer({ schema: 1, ok: false, error: "no managed server at /x" });

    expect(answer).toMatchObject({ ok: false, error: "no managed server at /x", repository: null });
    expect(answer.repositories).toEqual([]);
  });
});

describe("parseGitAccess", () => {
  it("reads the fingerprints and hosts, and nothing else", () => {
    const access = parseGitAccess({
      schema: 1,
      ok: true,
      error: null,
      ssh: {
        key: { type: "ED25519", fingerprint: "SHA256:key" },
        hosts: [{ host: "git.example.org", keys: [{ type: "ED25519", fingerprint: "SHA256:host" }] }],
      },
      https: { hosts: ["git.example.org"], username: "oauth2" },
    });

    expect(access.ssh?.key).toEqual({ kind: "ED25519", fingerprint: "SHA256:key" });
    expect(access.ssh?.hosts[0]?.keys[0]?.fingerprint).toBe("SHA256:host");
    expect(access.https).toEqual({ hosts: ["git.example.org"], username: "oauth2" });
  });

  it("reads a server without git access", () => {
    expect(parseGitAccess({ schema: 1, ok: true, error: null, ssh: null, https: null })).toEqual({
      ok: true,
      error: null,
      ssh: null,
      https: null,
    });
  });
});

describe("parseGitSources and parseRefStates", () => {
  it("keeps the git sources of a workspace", () => {
    const sources = parseGitSources({
      items: [
        {
          name: "app",
          kind: "repository",
          location: "/srv/local/app",
          refs: ["refs/heads/main"],
          schedule_seconds: 3600,
          include_paths: [],
          exclude_paths: ["**/*_test.go"],
        },
        { name: "chat", kind: "push", location: null, refs: [], schedule_seconds: null },
      ],
    });

    expect(sources).toEqual([
      {
        name: "app",
        location: "/srv/local/app",
        refs: ["refs/heads/main"],
        scheduleSeconds: 3600,
        exclude: ["**/*_test.go"],
        include: [],
      },
    ]);
  });

  it("says what each branch is doing", () => {
    const states = parseRefStates({
      items: [
        { key: "refs/heads/main", pending_sync: { status: "running", last_error: null }, last_run: null },
        { key: "refs/heads/next", pending_sync: null, last_run: null },
        {
          key: "refs/heads/old",
          pending_sync: null,
          last_run: { status: "failed", error: "the local source is outside MEMINI_GIT_LOCAL_ROOTS" },
        },
        { key: "refs/heads/done", pending_sync: null, last_run: { status: "succeeded", error: null } },
      ],
    });

    expect(states.map((state) => [state.tone, state.label])).toEqual([
      ["info", "syncing"],
      ["idle", "not synced yet"],
      ["bad", "failed"],
      ["good", "synced"],
    ]);
    expect(states[2]?.detail).toContain("MEMINI_GIT_LOCAL_ROOTS");
  });

  it("names the run of a sync that runs, and when the last one finished", () => {
    const states = parseRefStates({
      items: [
        {
          key: "refs/heads/main",
          pending_sync: { status: "running", last_error: null },
          last_run: { id: "01a2-run", status: "running", error: null, finished_at: null },
        },
        {
          key: "refs/heads/done",
          pending_sync: null,
          last_run: { id: "01a1-run", status: "succeeded", error: null, finished_at: "2026-11-25T20:14:00Z" },
        },
      ],
    });

    expect(states.map((state) => [state.running, state.runId, state.finishedAt])).toEqual([
      [true, "01a2-run", null],
      [false, "01a1-run", "2026-11-25T20:14:00Z"],
    ]);
  });
});

describe("the checks of what the user types", () => {
  it.each([
    ["/Users/sam/src/Harbour App/", "harbour-app"],
    ["https://git.example.org/harbour/app.git", "app"],
    ["git@git.example.org:harbour/Fleet_Tools.git", "fleet_tools"],
    ["", "repository"],
  ])("suggests a source name for %s", (from, expected) => {
    expect(suggestName(from)).toBe(expected);
  });

  it("checks a remote's form", () => {
    const form = { location: "https://git.example.org/harbour/app.git", name: "app", ref: "main" };
    expect(remoteProblem(form)).toBeNull();
    expect(remoteProblem({ ...form, location: "git@git.example.org:harbour/app.git" })).toBeNull();
    expect(remoteProblem({ ...form, location: "" })).toContain("address");
    expect(remoteProblem({ ...form, location: "ftp://git.example.org/app" })).toContain("https://");
    expect(remoteProblem({ ...form, name: "Bad Name" })).toContain("lowercase");
    expect(remoteProblem({ ...form, ref: "" })).toContain("branch");
  });

  it("tells ssh remotes and their hosts", () => {
    expect(isSshRemote("git@git.example.org:harbour/app.git")).toBe(true);
    expect(isSshRemote("ssh://git@git.example.org/harbour/app.git")).toBe(true);
    expect(isSshRemote("https://git.example.org/harbour/app.git")).toBe(false);
    expect(hostOf("git@Git.Example.org:harbour/app.git")).toBe("git.example.org");
    expect(hostOf("https://git.example.org/app.git")).toBe("git.example.org");
    expect(hostOf("not an address")).toBeNull();
  });

  it("checks hosts", () => {
    expect(splitHosts(" a.example.org, B.example.org  c.example.org")).toEqual([
      "a.example.org",
      "b.example.org",
      "c.example.org",
    ]);
    expect(hostsProblem("git.example.org")).toBeNull();
    expect(hostsProblem("")).toContain("Name the host");
    expect(hostsProblem("-oProxyCommand=x")).toContain("not a host name");
  });

  it("warns about Docker Desktop's file sharing for a folder outside /Users on macOS", () => {
    expect(mayNeedFileSharing("macos", "/Volumes/DATA/repos/app")).toBe(true);
    expect(mayNeedFileSharing("macos", "/Users/sam/src/app")).toBe(false);
    expect(mayNeedFileSharing("linux", "/srv/app")).toBe(false);
  });

  it("reads the schedule", () => {
    expect(readSchedule("3600")).toBe(3600);
    expect(readSchedule("")).toBeNull();
  });
});

describe("the commands", () => {
  it("passes the folder to Rust as the user typed it, and never a secret", async () => {
    const backend = createFakeBackend({
      answers: {
        launcher_repos: () => ({ schema: 1, ok: true, error: null, repository: REPO, repositories: [REPO] }),
        launcher_git: () => ({ schema: 1, ok: true, error: null, ssh: null, https: null }),
      },
    });

    await addFolder(backend, "/Users/sam/src/harbour-app");
    await setSshKey(backend, ["git.example.org"], { from: "file", path: "~/.ssh/deploy" });
    await setHttpsToken(backend, ["git.example.org"], " ", { from: "clipboard" });

    expect(backend.calls).toEqual([
      { command: "launcher_repos", args: { request: { action: "add", path: "/Users/sam/src/harbour-app" } } },
      {
        command: "launcher_git",
        args: {
          request: { action: "ssh_key", hosts: ["git.example.org"], source: { from: "file", path: "~/.ssh/deploy" } },
        },
      },
      {
        command: "launcher_git",
        args: {
          request: { action: "https_token", hosts: ["git.example.org"], username: null, source: { from: "clipboard" } },
        },
      },
    ]);
  });

  it("adds a git source with one branch and syncs it", async () => {
    const backend = createFakeBackend({
      answers: { api_source_create: () => ({ name: "app" }), api_source_sync: () => ({ jobs: [] }) },
    });

    await addSource(backend, "c1", {
      workspace: "harbour",
      name: "app",
      location: " /srv/local/app ",
      ref: " trunk ",
      scheduleSeconds: 3600,
      exclude: ["**/*_test.go"],
    });

    expect(backend.calls).toEqual([
      {
        command: "api_source_create",
        args: {
          id: "c1",
          source: {
            workspace: "harbour",
            name: "app",
            location: "/srv/local/app",
            refs: ["trunk"],
            schedule_seconds: 3600,
            exclude_paths: ["**/*_test.go"],
          },
        },
      },
      { command: "api_source_sync", args: { id: "c1", source: { workspace: "harbour", source: "app" } } },
    ]);
  });
});
