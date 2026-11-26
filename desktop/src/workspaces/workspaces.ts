// The workspace the whole app shows (memini-qpk.34). The server keeps each company or client
// apart in a workspace; the app chooses one in the sidebar, and every screen about the server's
// data shows that one. Pure: the hook in use-workspaces.ts asks Rust for the names.

/** What a workspace is and why to add one, for the screens and their empty states. */
export const WORKSPACE_MEANING =
  "A workspace is a separate space for one company or client: its code, mail, decisions and project map are kept apart from every other workspace, and a token can be limited to it.";

/**
 * The workspace to show: the one chosen, while the server still has it, or else the first one;
 * null when the server has none the token may read.
 */
export function currentWorkspace(names: readonly string[], chosen: string | undefined): string | null {
  if (chosen !== undefined && names.includes(chosen)) {
    return chosen;
  }
  return names[0] ?? null;
}
