# Memini desktop

A native application for macOS, Windows and Linux that manages a Memini server. It starts and stops
the server on this computer through the `memini-server` launcher, connects to other servers by
address and token, connects the agent clients on this computer to it in one click each, manages
tokens, shows what the base holds, runs the read tools by hand, and shows and edits the project map.

It is built with Tauri 2: a Rust side that owns the tokens, every HTTP call and the launcher, and a
React front end that shows what the Rust side returns. This is a first version; read
[What was verified](#what-was-verified) and [What is not done](#what-is-not-done) before relying
on it.

## Screens

| Screen | What it shows and does | What it needs |
|---|---|---|
| Connection | Saved connections: this computer, or a server by its address. Stores, replaces and removes a connection's token. **Test connection** shows each step with its state and what to do next: the address answers, the server's version, a token is stored, the server accepts it (refused, or the scope it lacks), and its scopes when the API tells them. The test also runs by itself when a connection is selected or saved or its token changes. | — |
| Server | The local server. **Set up** in one button: checks Docker, installs and starts the server (on 6364, or the next free port when that one is taken), creates its first admin token and makes this computer a connection, without a terminal. Then start, stop, restart and remove (with its data, after a confirmation), how many cores the search model may use while it indexes (half, background or all), and on a Mac whether it runs in Ollama on the Mac (about a hundred times faster, as Docker there has no GPU) or in Docker, with Ollama installed by Homebrew on request; the first setup settles this itself in its step **Prepare the search model**, the state of each service, and which launcher is used. After an upgrade, a bar per workspace while the server rebuilds its search index by itself. A server on this computer that was set up another way is shown as such: the app neither installs a second one nor manages it. **Git access**: one ssh key, with the fingerprints of the key and of its hosts' keys, and one https token with its hosts, each read by Rust from a file or the clipboard. **Folders on this computer**: the folders given to the server that no code base of any workspace reads, with Take back; a folder a code base reads is on its card. | Docker |
| MCP | The MCP address, whether the endpoint is on and the switch for the local server. The agent clients found on this computer, each with **Connect** and **Disconnect** (see [Agent clients](#agent-clients)). Configuration to paste by hand for any other client of [docs/mcp](../docs/mcp/README.md). | A connection; for Connect, a token with the `admin` scope |
| Tokens | Lists, creates and revokes tokens. A new token is shown once. | A token with the `admin` scope |
| Codebases | The code bases of the workspace chosen in the sidebar, as cards, with what a workspace is and **New workspace**. Each card: each branch's state; for a folder of this computer, the folder, where the server sees it and the branch checked out; the index of each branch (files, chunks, the commit and its date, from the stream's `index`), whatever the last run did; the last sync, from that run's counts and the stream's index (`Checked 12:26 — no changes; main is still at 0327a55, committed 1 Nov`, `Checked 12:26 — 3 changed, 1 deleted; main is at …`, or `full listing: N files written`); each branch's label comes from the stream's queued or running job (`queued`, `retrying`, `syncing`), and while one waits or runs the list is read every two seconds; while a sync runs, a bar labelled full listing or diff with the files done of all and the time left (from the run's `details.progress`); the project map's coverage (`GET …/sources/{source}/coverage`): not mapped, or mapped with the projects, their domains, the share of files in a domain, when and by which token the project was written, and the top folders in no domain; and Sync now, Pause or Resume, What it indexes and Delete. Sync now cannot be clicked while a sync of the code base is asked for, queued or running (it says `Asking…`, `Queued…`, `Syncing…`), and when the sync it asked for ends the card says how, branch by branch, in the line above. For a folder of this computer the card says that the server reads the folder's branch, not its remote: new commits appear after `git pull` in the working copy. How a full listing and a diff differ is said once above the cards. **Add a code base** opens a form: a folder of this computer, given to the local server by the launcher (read-only, with a warning about Docker Desktop's file sharing for a folder outside `/Users`), or a remote by its address, with what to leave out (tests, generated code and build output, data files, styles and assets, for the common languages; on by default) and globs of the user's own. **What it indexes** shows the same choices for a code base, counts what a sync would keep with them (by kind of file and top folder), and saves them and syncs again. **Map the code bases to the business** explains the three steps (copy, paste to an agent connected to Memini, check the Project map) and shows the task before it is copied. After an upgrade, a bar per workspace while the server rebuilds its search index (from `GET /v1/status`, `index.rebuild`). Scenarios [S17](../docs/scenarios/S17-add-a-repository.md), [S18](../docs/scenarios/S18-index-what-matters.md). | A token with the `admin` scope; a folder needs the local server |
| Data | What the base holds in the workspace chosen in the sidebar: each source with its kind, records and freshness. A console for the read tools: a form per tool, built from the arguments the server publishes, and the answer as cards and as raw JSON with the request that produced it. Per workspace, the records marked sensitive (left out of answers by default), with *Mark a record…* and *Unmark*. | A token with the `read` scope; `admin` to see and change marks |
| Project map | **Copy task for an agent** gives an agent connected to Memini the task of proposing the map from the code bases' Markdown and comments. The catalog of the workspace chosen in the sidebar as a tree: each project's services with their repository, directory and files, and its domains and subdomains with their path masks and what each mask matches now. **Edit** changes a project's title and description, its services (name, repository among the workspace's git sources, directory, description; adding and removing them), and its domains and subdomains with their path masks, and saves the whole project against the version it was read at, so a newer change is refused and not overwritten. A refusal of the server is said in the user's terms: a directory that is another project's service, a service another project names, a field that is not valid. **New project** creates an empty project, and refuses a name that exists. The links between services are shown in both directions, what the project's services use and which services of other projects use them; they are not edited here. | A token with the `read` scope; to save, `ingest` as well |

The sidebar holds the **workspace switcher** (`Workspace: harbour ▾`) under the brand, once the
connection's token may list workspaces: a workspace is a separate space for one company or client,
its code, mail, decisions and project map kept apart, and a token can be limited to it. Codebases,
Data and Project map show the workspace chosen there; Tokens and MCP are about the whole server.

A bar at the bottom of the window is always visible. It names the selected connection and says
whether the server answers, its version, whether the base is current or behind, and whether MCP is
on. A connection without a token, or with one the server refuses, is never shown as healthy: the
bar says **No token** or **Token refused** where the state of the base would stand, and Tokens,
Data and Project map show the same message with a button that leads to the token, instead of an
empty view. **Details** opens the base's status above the bar: each workspace, its streams, their lag and
their last run. The bar asks again every minute while the window is in front.

## Requirements

- Node 22.12 or later and pnpm 10. `package.json` names the exact pnpm version in `packageManager`.
- Rust 1.90 or later with `rustfmt` and `clippy`.
- On Linux, the system libraries Tauri builds against: `build-essential file
  libayatana-appindicator3-dev librsvg2-dev libssl-dev libwebkit2gtk-4.1-dev libxdo-dev`.
- To build the launcher that the app carries: [uv](https://docs.astral.sh/uv/), which gets
  Python 3.12 and PyInstaller into a throwaway environment.
- To manage a local server: Docker. The app carries the launcher; it needs no Homebrew, Scoop or
  Python on the user's computer.

The versions the app was last built with are in [the workflow](../.github/workflows/desktop-app.yml):
Node 24.22.0 and Rust 1.100.0.

## Develop

```sh
cd desktop
pnpm install
uv run --no-project --python 3.12 --with pyinstaller==6.22.3 python sidecar/freeze.py
pnpm tauri dev
```

The second command freezes the launcher for this machine into `src-tauri/binaries/` (see
[The launcher inside the app](#the-launcher-inside-the-app)). The Rust side does not build without
it. Run it again when `scripts/memini-server.py` changes.

`pnpm tauri dev` starts the Vite dev server on `127.0.0.1:1420` and the app's window on top of it.
It has not been run yet: see [What was verified](#what-was-verified).

The checks, as CI runs them:

```sh
pnpm typecheck
pnpm test
pnpm build
cd src-tauri
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
```

The Rust side embeds the built front end and bundles the frozen launcher, so run `pnpm build` and
`sidecar/freeze.py` once before the `cargo` commands.
The Rust tests use a keychain in memory: they never touch the keychain of the machine.

## Build

```sh
pnpm tauri build --bundles app -- --locked    # macOS: src-tauri/target/release/bundle/macos/Memini.app
```

The other bundle types are `dmg` (macOS), `deb` and `appimage` (Linux) and `nsis` (Windows). CI
builds them all ([desktop-app.yml](../.github/workflows/desktop-app.yml)), and the Release workflow
attaches them to the draft release of a tag. Add `--debug` for a faster build without
optimization. Nothing is signed.

## How it is put together

```
desktop/
  src/                  the front end, by feature
    components/         the component library: every control and surface, the tokens, the fonts
    hooks/              loading data and running an action, shared by the screens
    ipc/                the one bridge to the Rust side, and the error shape
    connections/  server/  mcp/  tokens/  data/  status/  project-map/   the screens
    json/  text/        readers for outside values, text made safe to show
  brand-src/            the mark: icon.svg for the app icon, glyph.svg for the sidebar
  public/licenses/      the licence notices of the bundled fonts, shipped with the app
  src-tauri/
    src/commands.rs     the commands the front end may call
    src/commands/checking.rs        the test of a connection, step by step
    src/commands/agent_clients.rs   Connect and Disconnect for agent clients
    src/clients.rs      the agent clients: where each keeps its configuration, and its memini entry
    src/jsonc.rs        JSON by position, to add or remove one member and keep every other byte
    fixtures/clients/   a hand-written configuration of each client, for the tests
    src/stub.rs         a stand-in server for the tests of the commands
    src/api.rs          the HTTP client of the Memini API
    src/launcher.rs     finding and running memini-server, reading its JSON
    src/tools.rs        the read tools of the Data screen and the request a call becomes
    src/keychain.rs     tokens in the operating system's keychain
    src/store.rs        the settings file with the saved connections
    src/secret.rs  src/error.rs   the token type, and errors whose text is masked
    capabilities/main-window.json   what the window may call
    tauri.conf.json     the window, the content security policy, the bundles
```

A new command is added in three places, or it cannot be called: the function in
`src-tauri/src/commands.rs` and its registration in `src-tauri/src/lib.rs`, the list in
`src-tauri/build.rs`, and `allow-<command>` in `src-tauri/capabilities/main-window.json`.

## Agent clients

The MCP screen lists the clients installed on this computer, among those the
[MCP guide](../docs/mcp/README.md) documents. A client counts as installed when its own directory
exists; one that is not installed is not offered. What the screen says about a client is read from
the client's file every time, never remembered.

| Client | Its file | The entry | The token |
|---|---|---|---|
| Claude Code | `~/.claude.json` | `mcpServers.memini` | variable, or in the file |
| Codex CLI | `~/.codex/config.toml` | `[mcp_servers.memini]` | variable only |
| Cursor | `~/.cursor/mcp.json` | `mcpServers.memini` | variable, or in the file |
| VS Code with GitHub Copilot | `~/.copilot/mcp-config.json` | `mcpServers.memini` | variable, or in the file |
| Gemini CLI | `~/.gemini/settings.json` | `mcpServers.memini` | variable, or in the file |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers.memini` | variable, or in the file |
| Zed | `settings.json` of Zed | `context_servers.memini` | in the file only |
| Cline | `cline_mcp_settings.json` in VS Code's storage of the extension | `mcpServers.memini` | variable, or in the file |
| OpenCode | `~/.config/opencode/opencode.jsonc` or `opencode.json` | `mcp.memini` (1.x) or `mcp.servers.memini` (2.x) | variable, or in the file |

**Connect**, on a click for one client, asks the server for a read token named for that client
(`Cursor (Memini desktop)`), limited to the workspace chosen on the screen, and writes the entry.
The entry is the one the MCP guide documents; a test compares each with the guide. An entry that
is there already is left as it is, and no token is made: Connect twice changes nothing.

**The token** is passed the safest way the client offers:

- *By the client's own reference to an environment variable* (the default where the client has
  one). The entry holds no token. The app keeps the token in
  `~/.config/memini/clients/<client>.token`, a file only the user can read, and the screen shows
  the line that loads it into the client's variable (`MEMINI_READ_TOKEN_CURSOR`, one variable per
  client, because each client has its own token). The client must be started from a shell that
  ran that line. A client started from the Dock or the Start menu does not see it.
- *In the client's file*, when the user ticks it, and always for Zed, whose configuration takes a
  literal value only. The app says so before it writes: the first click asks, naming the file. The
  file is then readable by the user only. Codex CLI is not offered this.

**Disconnect** removes the entry and the token file, and revokes the token the app made. An entry
made by hand is removed too; the app has no token on record for it and says that none was revoked.
When the server cannot be reached the entry still goes, the screen says that the token is still
valid, and offers to revoke it again.

**The rest of the file is kept.** In a JSON file the app changes the bytes of the `memini` member
and nothing else: comments, order, indentation and line ends stay, and Disconnect after Connect
gives the file back byte for byte. The Codex file is changed through a TOML editor that keeps the
rest as it was. The line ends of a file, those of Windows included, and a byte order mark at its
start are kept in both formats, and what the app adds is written with the file's own line ends. Before a change is written, the app checks that the new text says exactly what the
old text said plus or minus the entry, and refuses otherwise. A file that is not in the client's
format is refused and left alone, before any token is made. Before its first change the app copies
the file beside itself as `<file>.memini-backup`; that copy is never overwritten. A file that is a
link is changed where it points.

## Components

Every screen is built from the component library in `src/components/`, imported through its one
entry point, `src/components/index.ts`.

**The rule.** Reuse an existing component. If one is close, change or extend it with a prop or a
variant before making a new one. Create a new component only when none fits; it then goes into
`src/components/`, is exported from `index.ts`, comes with its test beside it, and is added to the
catalog below in the same change.

What follows from it, and what `src/components/library-rule.test.ts` checks on the source:

- A file outside `src/components/` holds no raw `<button>`, `<input>`, `<select>`, `<textarea>`,
  `<table>`, `<label>`, `<fieldset>`, `<form>`, `<pre>` or `<svg>`, and no styling of its own: no
  `className`, no inline `style`, no style sheet. When the test fails, fix the screen, not the test.
- Components are imported from `src/components` only, never from a file inside it. The entry file
  `src/main.tsx` alone loads the library's style sheets.
- Every component of the library is exported from `index.ts` and has a test beside its file.
- An action shows that it runs and how it ended (see Actions in `.claude/rules/desktop.md`): a
  `Button` or `IconButton` that submits a form, runs a `useAction` or reloads from the server has
  `busy`, and every `useAction` of a screen has its error or outcome shown. `ConfirmButton` cannot
  be written without `working`, and `useAction` itself never runs an action twice at once.

**Tokens.** Colours, radii, shadows and fonts are CSS variables in `src/components/tokens.css`,
for the light and the dark theme. `components.css` styles every component from them, and no other
file writes a colour, a radius or a shadow. `src/components/tokens.test.ts` checks that, and that
every text colour has a contrast of at least 4.5 to 1 against each ground it stands on, in both
themes.

**The catalog.**

| Component | What it is for |
|---|---|
| `Button` | Every button. One size; the variant (`secondary`, `primary`, `danger`) changes colours only. `submit` for the button of a form. `busy` (such as `"Syncing…"`): the action it started runs, so it shows those words with a turning ring and cannot be clicked. |
| `IconButton` | A square button that shows an icon and names itself for a screen reader; `busy` as for `Button`, a ring in place of the icon. |
| `ConfirmButton` | An action that cannot be taken back: the first click asks, the second acts. `working` (such as `"Deleting…"`) is shown from the second click until the action settles. |
| `CopyButton` | Copies a text through the Rust side and says that it did; `secret` for a token. |
| `TextInput` | A line of text: `prose`, `code` (an address, a path, an id: mono, uncorrected) or `number`. |
| `TextArea` | Several lines of literal text, such as a list with one value per line. |
| `Select` | One of several options. |
| `Checkbox` | A box to tick, with its words. |
| `Toggle` | A switch for one thing that is on or off and changes at once, with its label beside it. `busy` while the change runs and `unavailable` with the reason; both are read out, and the switch stays reachable by keyboard. Use it instead of a pair of on and off buttons. |
| `Field` | A control with its label, a note beside the label and a hint under the control. |
| `FieldGroup` | Several controls that answer one question, under a legend. |
| `Form` | A form that calls `onSubmit` and never navigates; `inline` for a field with its button. |
| `Screen` | The frame of a screen: its name and one sentence on what it is for. |
| `Panel` | A titled surface for one subject of a screen, with actions beside the title. |
| `Section` | A part of a panel with a heading of its own. |
| `Card` | One item of a list of results. |
| `EmptyState` | What stands where there is nothing to show yet. |
| `Notice`, `ErrorNotice` | A message about a state, by tone; an error of a command with its advice and reference. |
| `ActionOutcome` | How an action of `useAction` ended: its error, or what it did (the words its function returned). |
| `Pill` | A status chip: a few words coloured by their tone. |
| `ProgressBar` | How far a long piece of work is: a bar that fills, with its words under it; it slides while the total is not known. |
| `DataTable` | A table with headed columns that scrolls sideways instead of the page. |
| `Facts`, `Fact` | A list of named values. |
| `CodeBlock` | Literal text in mono, with a copy button when labelled; `wrap`, `bounded`, `secret`. |
| `Tabs` | A row of choices of which one is shown. |
| `RowList`, `SelectableRow` | A list from which one item is chosen. |
| `Text` | A paragraph or a run of text: `muted`, `hint`, `failure`, `small`, `inline`. |
| `Mono` | An address, a path or another literal value, in the mono font. |
| `Actions`, `Stack`, `Columns`, `FieldGrid`, `NoWrap` | Layout: a row of buttons, things one under the other, two fields side by side, a grid of fields, a value kept on one line. |
| `AppFrame`, `Splash` | The window: sidebar, screen and status bar; and what it shows before the app has loaded. |
| `Sidebar`, `Brand`, `NavItem` | The navigation: the glyph and the name, a header under them, and one item per screen. |
| `SidebarPicker` | A choice in the sidebar's colours that every screen follows, with its label and no line under it: the workspace. |
| `StatusBarFrame`, `StatusBarActions` | The bar at the bottom of the window, its details, and its buttons. |
| `Glyph`, `Icon` | The brand's mark in the colour of its text, and the small line icons. |
| `StepList` | The steps of a process in their order, each with its state: waiting, running, done or failed. |
| `Tree`, `TreeItem` | Things inside things: an item has a kind, a title, a count at the end of its line, a description, and the items inside it. |

## Brand

The brand is direction B, "Meander". Its sources are in `brand-src/`:

- `icon.svg` is the app icon. The icon set in `src-tauri/icons/` is generated from it and never
  edited by hand:

  ```sh
  pnpm tauri icon brand-src/icon.svg
  ```

  The command also writes icons for mobile and for the Windows Store, which the desktop bundles do
  not use; keep `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.icns`, `icon.ico` and
  `icon.png` and remove the rest.
- `glyph.svg` is the mark in one colour. The `Glyph` component reads its drawing from this file
  when the app is built, so the sidebar follows the file.

The interface is set in IBM Plex Sans; the name, code, paths and addresses in IBM Plex Mono. The
font files are bundled with the app from the pinned `@fontsource` packages (Latin, extended Latin
and Cyrillic; `src/components/fonts.css`), because the content security policy allows no remote
font. Both families are licensed under the SIL Open Font License 1.1; their notices are in
`public/licenses/` and are shipped inside the app. `src/components/fonts.test.ts` keeps the notices
equal to the packages' own.

## Security rules

These are the rules the code is built around. Tests hold most of them; change the test with the
rule, not around it.

- **A token never reaches the front end.** There is no field to type a token into. The Rust side
  reads it from the clipboard (and clears the clipboard) or from a file the user names, and puts it
  in the operating system's keychain: Keychain Services on macOS, the Credential Manager on Windows,
  the Secret Service on Linux. The front end learns only that a token is stored. The one exception
  is a newly created token, which the API itself shows once: it is shown until the user closes it
  and copied as a secret.
- **Every HTTP call is made in Rust**, to the address of a saved connection and nowhere else.
  Redirects are not followed. The front end makes no request, opens no link and stores nothing
  (`src/ipc/boundary.test.ts`).
- **A token belongs to the server it was stored for.** Changing a connection's scheme, host or port
  removes its token. The connection of this computer keeps it between ports of this computer,
  because the launcher's server can come back on another port.
- **Plain HTTP to another computer** needs a tick per connection: the token could be read on the
  network.
- **The app runs one program**, `memini-server`, with one of seven fixed argument vectors
  (`status`, `start`, `stop`, `mcp on`, `mcp off`, `bootstrap`, `bootstrap --force`, each with
  `--json`), directly and never through a shell. The front end names an action; nothing it sends
  becomes an argument. A path the user sets must still end in `memini-server`. Connecting an agent
  client runs nothing: the app writes the client's file itself.
- **A token made for an agent client stays out of the front end too.** Rust asks the server for
  it, and writes it to the client's token file or, on the user's word, to the client's own file.
  The front end gets the state of the client; the settings keep the token's id, to revoke it.
- **An agent client's file is changed on a click, for one client, one entry only.** The front end
  names a client of a fixed table; no path, address or text of an entry comes from it. What a
  client's file says is shown and never followed: the app runs no client and no command a file
  names.
- **The project map writes one thing**: a whole project, to
  `PUT /v1/workspaces/{workspace}/projects/{project}`, and only with the version it was read at.
  Rust refuses a document without `if_version`. A new project is written with `if_version` 0, which
  the server accepts only while no project has that name. Titles, descriptions and the notes of
  links are data: they are shown as text and written back unchanged where the user did not edit
  them.
- **The Data screen only reads.** A tool is one of a fixed table of read tools in
  `src-tauri/src/tools.rs`, each with the `GET` route that serves it. The front end names a tool
  and gives values; they become encoded path segments and query pairs of that route and cannot
  name another. No write tool is in the table, and there is no query language. Which arguments a
  tool takes comes from the server's OpenAPI document, so a new filter needs no change of the app.
- **Text from a server or from the launcher is data.** It is shown as text, with control and
  text-reordering characters removed and its length bounded, and it never makes the app act: the
  app offers the launcher's address and the user clicks.
- **The window has the least it needs**: a content security policy that allows the app's own
  scripts and styles and the bridge to Rust, nothing remote; a capability that lists the app's
  commands and writing to the clipboard, nothing of Tauri's core; commands the capability does not
  list are removed from the build.
- **Nothing is logged.** Errors that reach the front end are built from the app's own words and the
  API's message, masked for anything that looks like a token.

## The launcher inside the app

The app does not reimplement the launcher and does not ask the user to install it. It carries
`scripts/memini-server.py`, the one implementation, frozen into one program per platform and bundled
as a Tauri sidecar (`bundle.externalBin`), next to the app's own executable.

`sidecar/freeze.py` makes it. It takes the launcher and the files it ships with from
`packaging/server/build.py`, the recipe of the Homebrew and Scoop packages, so the compose files are
those of the launcher's pinned release, checked against its own checksums. PyInstaller freezes them
into `src-tauri/binaries/memini-server-<target triple>`, which is not committed. The script ends by
running the frozen program and the script side by side, without Docker, and fails unless both print
the same JSON and name the image the launcher pins. In a release, `--release-image` passes the image
the release has just published, with its digest; the copy being frozen pins it, with the compose
files of that tag, so the app of a release installs the server of the same release
([releasing](../docs/releasing.md#2-tag)). Only `ghcr.io/npavlov/memini:<version>@sha256:<digest>` is
accepted. PyInstaller does not cross-compile, so each platform's sidecar is built on that
platform: the workflow does it on its three runners.

Which launcher runs: a path the user set on the Server screen, else the one inside the app, else
one installed on this computer. The Server screen says which.

## The launcher contract

The app reads one JSON object from the launcher's standard output per command and refuses a
`schema` other than 1:

- `memini-server status --json`: `{"schema": 1, "installed", "running", "api_url", "mcp_url",
  "mcp_enabled", "image", "directory", "docker": {"available", "error"}, "services": [{"name",
  "state", "health"}]}`.
- `memini-server start|stop --json` and `memini-server mcp on|off --json`:
  `{"schema": 1, "ok", "error"}`. Restart is a stop and then a start.
- `memini-server bootstrap [--force] --json`: `{"schema": 1, "ok", "error", "token"}`, the
  first admin token of the server. Rust reads it from the launcher's pipe, puts it in the
  keychain and makes the local connection; the command's answer to the front end says only
  whether it worked.

An app started from the desktop gets a shorter `PATH` than a terminal, so the directories package
managers and Docker install their commands into are added to the `PATH` the launcher runs with:
the launcher finds `docker` there. One change runs at a time; while it runs the other buttons are
disabled and the screen says why, and the services are read again every few seconds. After a
change the app reads the status again, because recreating the service can change its port.

## Where the app keeps things

- Settings, without secrets: `settings.json` in the app's configuration directory
  (`~/Library/Application Support/io.github.npavlov.memini` on macOS,
  `~/.config/io.github.npavlov.memini` on Linux, `%APPDATA%\io.github.npavlov.memini` on Windows).
- Tokens: the keychain, under the service `Memini Desktop`, one entry per connection.
- For each agent client it connected by a variable: the client's token in
  `~/.config/memini/clients/<client>.token`, readable by the user only, until Disconnect. The
  settings keep the id of the token and of the connection it was made with, never the token.
- Beside each client's file it changed: `<file>.memini-backup`, the file as it was before the
  app's first change.

## What was verified

On macOS (Apple Silicon), 2026-11-25, with Node 24.22.0, pnpm 10.30.3, Rust 1.100.0 and uv 0.10.0:

| Command | Result |
|---|---|
| `uv run … python sidecar/freeze.py` | sidecar built; frozen and script print the same JSON |
| `pnpm typecheck` | no errors |
| `pnpm test` | 888 tests passed |
| `pnpm build` | built |
| `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings` | clean |
| `cargo test --locked` | 125 tests passed |
| `pnpm tauri build --bundles app -- --locked` | `Memini.app` built, with the launcher inside |

The built front end was also opened in a browser with a stand-in for the Rust side, under the
production content security policy and with `Object.prototype` frozen as Tauri freezes it: the
screens rendered without an error, and markup in a server's text stayed text. The launcher's new
`bootstrap` command was run against a stand-in for Docker.

Connect and Disconnect are tested on a fixture copy of each client's configuration
(`src-tauri/fixtures/clients/`), and from the command down against a stand-in server that answers
the two token routes: the entry, the token file and its permissions, the copy made before the
first change, the one request to make a token and the one to revoke it, and that the token is in
nothing the front end or the settings get. The project map was opened in the browser check with a
stand-in catalog: the tree with its counts, an edit, a save the stand-in refused as a version
conflict, and a save it accepted.

The connection test is tested from the command down against a stand-in server, one case each: an
address nothing listens at, a server without a token stored, a refused token, a token without the
read scope, an accepted one, and an address that answers but is not a Memini server. In the
browser check a connection without a token showed **No token** in the bar and on the Data screen,
the button led to the token, and the MCP switch was worked with Space and Enter.

## What is not done

- **Nothing here was run against a real Docker or a real server.** The setup, start, stop, restart,
  the MCP switch and the first token were tested with stand-ins: the launcher's commands against a
  stub of Docker, the app's commands against a stub of the launcher. `pnpm tauri dev` was not run.
- **Windows and Linux were not compiled**, and their sidecars were not built: the code and the
  workflow step are written and wait for the first CI run.
- **The scopes of a token are shown only when the server tells them**, which it does to a token
  with the `admin` scope, in its list of tokens. For another token the test says that it may read,
  or which scope the server asked for, and no more.
- **The MCP switch acts at once.** It was a button that asked first; the switch restarts the
  server on one click, and says so beside it.
- **The link to Docker's install page is text to copy.** The app opens no link: that would need a
  capability it does not have.
- **No agent client was started against an entry the app wrote.** The entries are the documented
  ones, and the files were fixtures. A client's own rules can differ from its documentation: which
  file wins when several exist, whether the file may hold comments, and how a client that is
  running treats a file changed under it. Claude Code rewrites `~/.claude.json` while it runs, so
  connect it while it is closed.
- **Agent clients, what is left out.** Only the user's own configuration of each client is
  handled, not a project's (`.mcp.json`, `.cursor/mcp.json`), and not a directory moved with
  `CODEX_HOME` or `COPILOT_HOME`. The version of OpenCode is asked when its file does not tell; the
  app does not run `opencode --version`. Devin Local and Pi are not offered. The line that loads a
  token into a client's variable is shown to copy; the app does not edit a shell profile.
- **The project map was not run against a real server.** It reads `get_catalog`, `get_project` and
  `get_domain` and writes `put_project` as they are on `main`; the server's refusals were shown
  from a stand-in. A project cannot be deleted here, and links are shown but not edited: a save
  sends them back as they were read, so that it does not remove them.
- **A service added in the editor can be named by a domain after the project is saved with it**,
  not before: what names a service keeps the name the service was read with until the save, which
  is what lets a service be renamed without losing its code. A renamed service is a new one to the
  server, so it refuses the rename while another project names the service.
- **Signing, notarization and updates.** A release attaches unsigned bundles, which the system
  asks about before the first start ([Desktop app](../docs/install.md#desktop-app)); a frozen
  launcher inside a signed app will need signing too. The app does not update itself. The application identifier
  should be confirmed before the first release: changing it later moves the settings.
- **`list_commits`** is in the table of read tools with its route, `GET /v1/commits`. The Data
  screen offers it once the connected server's OpenAPI document has that route; it was not tried
  against such a server.
- **Chunks per source.** The overview counts chunks per workspace; no route counts them per source.
