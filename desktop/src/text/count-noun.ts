/** A count with its noun in the singular or the plural: `1 stream`, `2 streams`. */
export function countNoun(count: number, singular: string, plural: string = `${singular}s`): string {
  return `${String(count)} ${count === 1 ? singular : plural}`;
}
