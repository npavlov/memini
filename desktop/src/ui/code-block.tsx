import type { ReactElement } from "react";

import { CopyButton } from "./copy-button";

export interface CodeBlockProps {
  /** What the text is or where it goes. */
  readonly label: string;
  readonly text: string;
}

/** Text to copy, shown exactly as it will be copied. */
export function CodeBlock({ label, text }: CodeBlockProps): ReactElement {
  return (
    <figure className="code-block">
      <figcaption>
        <span>{label}</span>
        <CopyButton text={text} />
      </figcaption>
      <pre>
        <code>{text}</code>
      </pre>
    </figure>
  );
}
