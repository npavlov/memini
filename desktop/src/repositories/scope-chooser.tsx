import type { ReactElement } from "react";

import { Checkbox, Field, FieldGroup, TextArea } from "../components";
import { SCOPE_PRESETS, splitGlobs } from "./codebases";
import type { PresetId } from "./codebases";

/** What a code base indexes, as the user chooses it: presets to leave out, and globs of their own. */
export interface Scope {
  readonly presets: ReadonlySet<PresetId>;
  /** The user's globs as typed, one per line. */
  readonly custom: string;
}

export interface ScopeChooserProps {
  readonly scope: Scope;
  readonly onChange: (scope: Scope) => void;
}

/** The parts of a repository to leave out of the index. Markdown and source code stay in. */
export function ScopeChooser({ scope, onChange }: ScopeChooserProps): ReactElement {
  return (
    <>
      <FieldGroup label="Leave out" hint="Markdown and source code stay in: they explain what the code does.">
        {SCOPE_PRESETS.map((preset) => (
          <Checkbox
            key={preset.id}
            checked={scope.presets.has(preset.id)}
            onChange={(checked) => {
              const presets = new Set(scope.presets);
              if (checked) {
                presets.add(preset.id);
              } else {
                presets.delete(preset.id);
              }
              onChange({ ...scope, presets });
            }}
          >
            {preset.label}: {preset.hint}
          </Checkbox>
        ))}
      </FieldGroup>
      <Field
        label="Also leave out"
        note="optional"
        hint="One path glob per line: ** any folders, * any characters in a name. Example: legacy/**"
      >
        <TextArea
          value={scope.custom}
          rows={3}
          onChange={(custom) => {
            onChange({ ...scope, custom });
          }}
        />
      </Field>
    </>
  );
}

/** The globs a scope adds of the user's own. */
export function customGlobs(scope: Scope): readonly string[] {
  return splitGlobs(scope.custom);
}
