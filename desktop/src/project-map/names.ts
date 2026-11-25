// The names of the catalog: a project, a service, a domain. The server checks them again; these
// checks only tell the user before a save what it will say.

const NAME = /^[a-z0-9][a-z0-9_-]{0,62}$/u;

export const NAME_RULE = "lowercase letters, digits, '-' or '_', starting with a letter or a digit, at most 63 characters";

export function isName(text: string): boolean {
  return NAME.test(text.trim());
}

/** The first problem with the names of one kind of thing, or null: one that is not a name, or two alike. */
export function checkNames(names: readonly string[], what: string): string | null {
  if (names.some((name) => !isName(name))) {
    return `Every ${what} needs a name of ${NAME_RULE}.`;
  }
  const trimmed = names.map((name) => name.trim());
  const repeated = trimmed.find((name, index) => trimmed.indexOf(name) !== index);
  return repeated === undefined ? null : `Two ${what}s are named ${repeated}.`;
}
