# S17. Add a repository without a terminal

| Area | Who | Status | Issue |
|---|---|---|---|
| Running Memini | Operator, engineer | ready | memini-qpk.18 |

## Who and pain

The operator set up Memini with the desktop app and now wants it to know their code. The app has no
way to add a repository. The server runs in Docker and sees neither the repositories on this
computer nor the key that reads a private remote. Today that takes a terminal, `memini source add`, a
hand-edited Compose file and secret files.

## Question

"Follow the repository in `/Users/sam/src/harbour`, and the private one at
`git@git.example.org:harbour/fleet.git`."

## Answer

The Codebases screen of the desktop app, for the workspace chosen in the sidebar (memini-qpk.34).
The switcher under the brand holds for every screen and says what a workspace is: a separate space
for one company or client, its code, mail, decisions and project map kept apart; a token can be
limited to it. A server without one offers **New workspace** first.

- **A folder on this computer.** It is offered for the local connection. The app checks that the
  folder holds a git repository, then `memini-server repos add FOLDER`. The launcher lists the
  folder in the state directory and writes `compose.local.yaml`, which mounts it read-only at
  `/srv/local/<name>` and sets `MEMINI_GIT_LOCAL_ROOTS=/srv/local`. It recreates the service and
  looks into the mount. An empty mount is taken back, with the advice to share the folder in
  Docker Desktop: this happens to a folder Docker Desktop does not share. The app then adds a git
  source at that location, following the checked-out branch unless another is named, and syncs it.
- **A repository at an address.** An `https://`, `ssh://` or `git@host:path` address with a branch;
  the app adds the git source and syncs it.
- **The list.** Each git source of the workspace, with each branch's state (syncing, synced,
  failed with the reason, not synced yet), Sync now and Delete. A mounted folder is the server's,
  not a workspace's: it is shown on the card of the code base that reads it (the folder on this
  computer, where the server sees it, the branch checked out). Never a source of another
  workspace.
- **Folders on this computer**, on the Server screen: the folders given to the server that no
  code base of any workspace reads, whether each is still there, and Take back.

On the Server screen, **Git access** gives the server one ssh key and one https token
(`memini-server git ssh-key|https-token|remove`). For an ssh key the launcher fetches the host keys
of the named hosts. The app shows their fingerprints, and those of the key, to compare with the
ones the git server publishes. The key or token is read by Rust from a file or the clipboard and
reaches the launcher on its standard input. It is never in an argument and never in the window.

## Data

No new data. The source is an ordinary git source (requirements, section 5, **Native connector**).
The service syncs its mirror on its schedule, from the folder or the remote. A folder is read
through `upload-pack`, never written. New commits show once they are committed or pulled in that
folder.

## Check

- `tests/test_server_manager.py`:
  - `repos add` writes a read-only bind mount and the local root, and recreates the service.
  - It refuses a path that is not a repository and a linked worktree.
  - It takes back a folder Docker mounts empty, or refuses to mount.
  - `git ssh-key` keeps the key with mode 600 and the scanned host keys. It prints fingerprints only
    and refuses a key with a passphrase.
  - `git https-token` keeps the token for its hosts and never prints it.
- `desktop/src-tauri/src/local_git.rs`: a folder is resolved and must be a repository. A host can
  never be an option. Every argument is one element, and no secret is an argument.
- `desktop/src/repositories/repositories.test.ts`: the answers are parsed, and the forms checked.
  Adding a source sends one branch and syncs it.
- `desktop/src/repositories/codebase-state.test.ts`: a folder is found by where the server sees
  it, and the Server screen lists only the folders no source reads.
- `desktop/src/workspaces/workspaces.test.ts`: the workspace shown is the one chosen while it
  exists, else the first.
- By hand, on the release candidate: add a folder under `/Users` and one on another disk, and add
  a public remote. Each is synced and found by `search_code`.

## Notes

- On Linux the service user (uid 10001) must be able to read the folder and the ssh key. The
  launcher cannot change their owner. See [Install](../install.md#repositories-and-git-access).
- Credentials are one per server, as the service has them (requirements, section 6). A credential
  per source comes with the credential store.

## See also

- [All scenarios](README.md)
- [S11](S11-run-without-a-terminal.md): Run Memini without a terminal
- [S15](S15-see-what-memini-holds.md): See what Memini holds and try its tools
