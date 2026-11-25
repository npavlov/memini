# Memini desktop

A native application for macOS, Windows and Linux that manages a Memini server. It starts and stops
the server on this computer through the `memini-server` launcher, connects to other servers by
address and token, shows the MCP address with configuration to paste into a client, manages tokens,
and shows whether the sources are current.

It is built with Tauri 2: a Rust side that owns the tokens, every HTTP call and the launcher, and a
React front end that shows what the Rust side returns. This is a first version; read
[What was verified](#what-was-verified) and [What is not done](#what-is-not-done) before relying
on it.

## Screens

| Screen | What it shows and does | What it needs |
|---|---|---|
| Connection | Saved connections: this computer, or a server by its address. Stores, replaces and removes a connection's token. Checks a connection. | — |
| Server | The local server: whether Docker is available, whether the server is installed and running, each service, start and stop. Explains how to install the launcher when it is missing. | The `memini-server` launcher |
| MCP | The MCP address, whether the endpoint is on, the switch for the local server, and configuration for the clients of [docs/mcp](../docs/mcp/README.md). | A connection |
| Tokens | Lists, creates and revokes tokens. A new token is shown once. | A token with the `admin` scope |
| Status | Whether the base is current; each stream's lag, schedule and last run. | A token with the `read` scope |
| Project map | A placeholder. The server has no catalog API yet. | — |

## Requirements

- Node 22.12 or later and pnpm 10. `package.json` names the exact pnpm version in `packageManager`.
- Rust 1.90 or later with `rustfmt` and `clippy`.
- On Linux, the system libraries Tauri builds against: `build-essential file
  libayatana-appindicator3-dev librsvg2-dev libssl-dev libwebkit2gtk-4.1-dev libxdo-dev`.
- To manage a local server: Docker and the launcher, installed as the
  [repository README](../README.md#install) says. The app contains neither.

The versions the app was last built with are in [the workflow](../.github/workflows/desktop.yml):
Node 24.22.0 and Rust 1.100.0.

## Develop

```sh
cd desktop
pnpm install
pnpm tauri dev
```

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

The Rust side embeds the built front end, so run `pnpm build` once before the `cargo` commands.
The Rust tests use a keychain in memory: they never touch the keychain of the machine.

## Build

```sh
pnpm tauri build --bundles app -- --locked    # macOS: src-tauri/target/release/bundle/macos/Memini.app
```

The other bundle types are `deb` (Linux) and `nsis` (Windows); `dmg` and `appimage` are configured
but nothing builds them yet. Add `--debug` for a faster build without optimization. Nothing is
signed.

## How it is put together

```
desktop/
  src/                  the front end, by feature
    ipc/                the one bridge to the Rust side, and the error shape
    connections/  server/  mcp/  tokens/  status/  project-map/
    json/  text/  ui/   readers for outside values, text made safe to show, shared components
  src-tauri/
    src/commands.rs     the commands the front end may call
    src/api.rs          the HTTP client of the Memini API
    src/launcher.rs     finding and running memini-server, reading its JSON
    src/keychain.rs     tokens in the operating system's keychain
    src/store.rs        the settings file with the saved connections
    src/secret.rs  src/error.rs   the token type, and errors whose text is masked
    capabilities/main-window.json   what the window may call
    tauri.conf.json     the window, the content security policy, the bundles
```

A new command is added in three places, or it cannot be called: the function in
`src-tauri/src/commands.rs` and its registration in `src-tauri/src/lib.rs`, the list in
`src-tauri/build.rs`, and `allow-<command>` in `src-tauri/capabilities/main-window.json`.

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
- **The app runs one program**, `memini-server`, with one of five fixed argument vectors
  (`status`, `start`, `stop`, `mcp on`, `mcp off`, each with `--json`), directly and never through
  a shell. The front end names an action; nothing it sends becomes an argument. A path the user
  sets must still end in `memini-server`.
- **Text from a server or from the launcher is data.** It is shown as text, with control and
  text-reordering characters removed and its length bounded, and it never makes the app act: the
  app offers the launcher's address and the user clicks.
- **The window has the least it needs**: a content security policy that allows the app's own
  scripts and styles and the bridge to Rust, nothing remote; a capability that lists the app's
  commands and writing to the clipboard, nothing of Tauri's core; commands the capability does not
  list are removed from the build.
- **Nothing is logged.** Errors that reach the front end are built from the app's own words and the
  API's message, masked for anything that looks like a token.

## The launcher contract

The app reads one JSON object from the launcher's standard output per command and refuses a
`schema` other than 1:

- `memini-server status --json`: `{"schema": 1, "installed", "running", "api_url", "mcp_url",
  "mcp_enabled", "image", "directory", "docker": {"available", "error"}, "services": [{"name",
  "state", "health"}]}`.
- `memini-server start|stop --json` and `memini-server mcp on|off --json`:
  `{"schema": 1, "ok", "error"}`.

The launcher is looked for on `PATH`, then where Homebrew and Scoop install it. An app started from
the desktop gets a shorter `PATH` than a terminal, so those directories and the ones Docker installs
its command into are added to the `PATH` the launcher runs with. After a change the app reads the
status again, because recreating the service can change its port.

## Where the app keeps things

- Settings, without secrets: `settings.json` in the app's configuration directory
  (`~/Library/Application Support/io.github.npavlov.memini` on macOS,
  `~/.config/io.github.npavlov.memini` on Linux, `%APPDATA%\io.github.npavlov.memini` on Windows).
- Tokens: the keychain, under the service `Memini Desktop`, one entry per connection.

## What was verified

On macOS (Apple Silicon), 2026-11-25, with Node 24.22.0, pnpm 10.30.3 and Rust 1.100.0:

| Command | Result |
|---|---|
| `pnpm typecheck` | no errors |
| `pnpm test` | 272 tests passed |
| `pnpm build` | built |
| `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings` | clean |
| `cargo test --locked` | 53 tests passed |
| `pnpm tauri build --debug --bundles app -- --locked` | `Memini.app` built |
| `pnpm tauri build --bundles app -- --locked` | `Memini.app` built |

The built front end was also opened in a browser with a stand-in for the Rust side, under the
production content security policy and with `Object.prototype` frozen as Tauri freezes it: every
screen rendered without an error, and markup in a server's text stayed text.

## What is not done

- **The app was never started.** It compiles and bundles on macOS; nobody has opened its window,
  clicked through it against a real server, or stored a token in a real keychain. `pnpm tauri dev`
  was not run.
- **Windows and Linux were not compiled.** The code for them (finding the launcher, starting it
  without a console window, the Secret Service) is written and waits for the first CI run.
- **The launcher's `--json` output and `mcp on|off`** arrive with another change. The app was
  tested against the contract above, not against the launcher.
- **Signing, notarization, published installers and updates.** CI builds unsigned bundles to look
  at. The icon is a placeholder, and the application identifier should be confirmed before the
  first release: changing it later moves the settings.
- **The first admin token still comes from a terminal** (`memini-server cli bootstrap`). The
  launcher contract has no command for it.
- **The project map**, code search and repositories: the server has no catalog API yet.
