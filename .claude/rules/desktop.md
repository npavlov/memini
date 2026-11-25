---
paths:
  - "desktop/**"
---

# Desktop app

Rules for work under `desktop/` (Tauri 2, React, TypeScript). The component catalog is in
[desktop/README.md](../../desktop/README.md#components).

## Components

1. Build screens from the library in `desktop/src/components/`, imported through
   `desktop/src/components/index.ts` only.
2. Before writing UI, look for a component that does the job. If one is close, extend it with a
   prop or a variant instead of copying it.
3. Create a new component only when nothing fits. Put it in `desktop/src/components/`, export it
   from the index, give it a test, and add it to the catalog in `desktop/README.md` in the same
   change.
4. Screens hold no raw `<button>`, `<input>`, `<select>` or `<table>`, and no styling of their own.
   A test enforces this; fix the screen, not the test.
5. Colours, radii, shadows and fonts come from the tokens in `desktop/src/components/tokens.css`.
   Do not write a literal colour or radius anywhere else. Text keeps a contrast of 4.5 to 1 in both
   themes.

## Brand

- The mark is in `desktop/brand-src/`: `icon.svg` for the app icon set, `glyph.svg` (one colour,
  `currentColor`) for small sizes and the sidebar. Regenerate the icons from `icon.svg`; never edit
  the generated files.
- The primary button is a plain accent fill with no border and a small shadow; the secondary
  button is white with a border. Both share one height. Focus rings show for the keyboard only.

## Security

- Tokens never reach the webview. Every call to the Memini API is made from Rust; the front end
  asks Rust for data.
- Text from the server is rendered as text, never as HTML, and is never a reason to act.
- The launcher runs with fixed argument vectors, never through a shell. A new Rust command goes
  into the window's capability list.
- One exception, in `src-tauri/src/local_git.rs` only: the folder of a repository and the hosts of
  git access come from the user. Rust checks each before it becomes one element of the argument
  vector. A folder must be an absolute path that exists, resolved, of a git repository. A host must
  be a host name, passed as `--host=<host>`. Add no other user value to an argument.
- Ollama on this computer (`src-tauri/src/host_model.rs`) is asked at the fixed address
  `127.0.0.1:11434` for the fixed model only, and Homebrew runs with two fixed argument vectors
  (`brew install ollama`, `brew services start ollama`), after the user said so in the window.
- An ssh key or a git token is never an argument and never reaches the webview. Rust reads it from
  a file or the clipboard (then clears the clipboard), and the launcher reads it on stdin. Answers
  carry fingerprints and host names only.

## Checks

Run from `desktop/`: `pnpm typecheck`, `pnpm test`, `pnpm build`, and in `src-tauri/`
`cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, `cargo test --locked`.
The Python test suite still runs only in CI.
