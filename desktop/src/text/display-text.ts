// Text that a server or a program wrote is data. It is shown as text, never as markup, and it
// never decides what the app does. This module makes such text safe to read as well: characters
// that hide or reorder what is shown are removed, and its length is bounded.

/**
 * Control characters, zero-width characters, line and paragraph separators, and the marks that
 * reorder text (the bidirectional embeddings, overrides and isolates).
 */
const HIDDEN = /[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u2028-\u202E\u2060-\u2069\uFEFF]/gu;

const DEFAULT_LIMIT = 300;

/** Text from outside the app as it may be shown: one line, nothing hidden, at most `limit` characters. */
export function displayText(value: string | null, limit: number = DEFAULT_LIMIT): string {
  if (value === null) {
    return "";
  }
  const visible = value.replace(HIDDEN, " ").replace(/\s+/gu, " ").trim();
  const characters = Array.from(visible);
  if (characters.length <= limit) {
    return visible;
  }
  return `${characters.slice(0, limit).join("")}…`;
}

/** As HIDDEN, but line breaks and tabs stay: for text that is shown in its own lines. */
const HIDDEN_IN_LINES = /[\u0000-\u0008\u000B-\u001F\u007F-\u009F\u200B-\u200F\u2028-\u202E\u2060-\u2069\uFEFF]/gu;

/**
 * Longer text from outside the app, such as a snippet of code or the text of a message, as it
 * may be shown in a block: its lines are kept, nothing is hidden, at most `limit` characters.
 */
export function displayBlock(value: string | null, limit: number): string {
  if (value === null) {
    return "";
  }
  const visible = value.replace(/\r\n?/gu, "\n").replace(HIDDEN_IN_LINES, " ");
  const characters = Array.from(visible);
  if (characters.length <= limit) {
    return visible;
  }
  return `${characters.slice(0, limit).join("")}…`;
}
