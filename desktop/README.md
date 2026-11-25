# Memini desktop

A native application for macOS, Windows and Linux that manages a Memini server. It starts and stops
the server on this computer through the `memini-server` launcher, connects to other servers by
address and token, shows the MCP address with configuration to paste into a client, manages tokens,
shows what the base holds, and runs the read tools by hand.

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
| Data | What the base holds: per workspace, each source with its kind, records and freshness. A console for the read tools: a form per tool, built from the arguments the server publishes, and the answer as cards and as raw JSON with the request that produced it. Read-only. | A token with the `read` scope |
| Project map | A placeholder. The server has no catalog API yet. | — |

A bar at the bottom of the window is always visible. It names the selected connection and says
whether the server answers, its version, whether the base is current or behind, and whether MCP is
on. **Details** opens the base's status above the bar: each workspace, its streams, their lag and
their last run. The bar asks again every minute while the window is in front.

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
    components/         the component library: every control and surface, the tokens, the fonts
    hooks/              loading data and running an action, shared by the screens
    ipc/                the one bridge to the Rust side, and the error shape
    connections/  server/  mcp/  tokens/  data/  status/  project-map/   the screens
    json/  text/        readers for outside values, text made safe to show
  brand-src/            the mark: icon.svg for the app icon, glyph.svg for the sidebar
  public/licenses/      the licence notices of the bundled fonts, shipped with the app
  src-tauri/
    src/commands.rs     the commands the front end may call
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

**Tokens.** Colours, radii, shadows and fonts are CSS variables in `src/components/tokens.css`,
for the light and the dark theme. `components.css` styles every component from them, and no other
file writes a colour, a radius or a shadow. `src/components/tokens.test.ts` checks that, and that
every text colour has a contrast of at least 4.5 to 1 against each ground it stands on, in both
themes.

**The catalog.**

| Component | What it is for |
|---|---|
| `Button` | Every button. One size; the variant (`secondary`, `primary`, `danger`) changes colours only. `submit` for the button of a form. |
| `IconButton` | A square button that shows an icon and names itself for a screen reader. |
| `ConfirmButton` | An action that cannot be taken back: the first click asks, the second acts. |
| `CopyButton` | Copies a text through the Rust side and says that it did; `secret` for a token. |
| `TextInput` | A line of text: `prose`, `code` (an address, a path, an id: mono, uncorrected) or `number`. |
| `TextArea` | Several lines of literal text, such as a list with one value per line. |
| `Select` | One of several options. |
| `Checkbox` | A box to tick, with its words. |
| `Field` | A control with its label, a note beside the label and a hint under the control. |
| `FieldGroup` | Several controls that answer one question, under a legend. |
| `Form` | A form that calls `onSubmit` and never navigates; `inline` for a field with its button. |
| `Screen` | The frame of a screen: its name and one sentence on what it is for. |
| `Panel` | A titled surface for one subject of a screen, with actions beside the title. |
| `Section` | A part of a panel with a heading of its own. |
| `Card` | One item of a list of results. |
| `EmptyState` | What stands where there is nothing to show yet. |
| `Notice`, `ErrorNotice` | A message about a state, by tone; an error of a command with its advice and reference. |
| `Pill` | A status chip: a few words coloured by their tone. |
| `DataTable` | A table with headed columns that scrolls sideways instead of the page. |
| `Facts`, `Fact` | A list of named values. |
| `CodeBlock` | Literal text in mono, with a copy button when labelled; `wrap`, `bounded`, `secret`. |
| `Tabs` | A row of choices of which one is shown. |
| `RowList`, `SelectableRow` | A list from which one item is chosen. |
| `Text` | A paragraph or a run of text: `muted`, `hint`, `failure`, `small`, `inline`. |
| `Mono` | An address, a path or another literal value, in the mono font. |
| `Actions`, `Stack`, `Columns`, `FieldGrid`, `NoWrap` | Layout: a row of buttons, things one under the other, two fields side by side, a grid of fields, a value kept on one line. |
| `AppFrame`, `Splash` | The window: sidebar, screen and status bar; and what it shows before the app has loaded. |
| `Sidebar`, `Brand`, `NavItem` | The navigation: the glyph and the name, and one item per screen. |
| `StatusBarFrame`, `StatusBarActions` | The bar at the bottom of the window, its details, and its buttons. |
| `Glyph`, `Icon` | The brand's mark in the colour of its text, and the small line icons. |

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
- **The app runs one program**, `memini-server`, with one of five fixed argument vectors
  (`status`, `start`, `stop`, `mcp on`, `mcp off`, each with `--json`), directly and never through
  a shell. The front end names an action; nothing it sends becomes an argument. A path the user
  sets must still end in `memini-server`.
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
| `pnpm test` | 640 tests passed |
| `pnpm build` | built |
| `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings` | clean |
| `cargo test --locked` | 64 tests passed |
| `pnpm tauri icon brand-src/icon.svg` | icon set written |
| `pnpm tauri build --bundles app -- --locked` | `Memini.app` built |

The built front end was also opened in a browser with a stand-in for the Rust side, under the
production content security policy and with `Object.prototype` frozen as Tauri freezes it. Every
screen rendered without an error; markup in a server's text stayed text; the bundled fonts loaded;
every button measured the same height, padding, font and radius, the primary one without a border;
a click with the mouse drew no focus ring and the Tab key drew one; and the forms of the ten read
tools were built from the OpenAPI document of the service in this repository.

## What is not done

- **The app was not checked against a real server.** It was opened on macOS; nothing here has run
  it against a real server, launcher or keychain, and `pnpm tauri dev` was not run.
- **Windows and Linux were not compiled.** The code for them (finding the launcher, starting it
  without a console window, the Secret Service) is written and waits for the first CI run.
- **The launcher's `--json` output and `mcp on|off`** arrive with another change. The app was
  tested against the contract above, not against the launcher.
- **Signing, notarization, published installers and updates.** CI builds unsigned bundles to look
  at. The application identifier should be confirmed before the first release: changing it later
  moves the settings.
- **The first admin token still comes from a terminal** (`memini-server cli bootstrap`). The
  launcher contract has no command for it.
- **`list_commits`** is in the table of read tools with the route `GET /v1/commits`, which is a
  guess: the server does not have the tool yet. The Data screen offers it once the server's
  OpenAPI document has that route.
- **Chunks per source.** The overview counts chunks per workspace; no route counts them per source.
- **The project map** and repositories: the server has no catalog API yet.
