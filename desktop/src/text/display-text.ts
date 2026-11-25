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
