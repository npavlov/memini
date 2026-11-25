import type { ReactElement, ReactNode } from "react";

import { CopyButton } from "./copy-button";

export interface CodeBlockProps {
  /** What the text is or where it goes. Without a label there is no copy button either. */
  readonly label?: string;
  readonly text: string;
  /** Wraps long lines instead of scrolling sideways: for prose-like text and for a secret. */
  readonly wrap?: boolean;
  /** Marks the block as something to keep safe, and copies it as a secret. */
  readonly secret?: boolean;
  /** Caps the height; the block then scrolls. */
  readonly bounded?: boolean;
}

function classOf(props: CodeBlockProps): string {
  const parts = [
    "code",
    props.wrap === true || props.secret === true ? "code-wrap" : null,
    props.secret === true ? "code-secret" : null,
    props.bounded === true ? "code-bounded" : null,
  ];
  return parts.filter((part) => part !== null).join(" ");
}

/** Literal text in the mono font, shown exactly as it is: a command, a file, a JSON answer. */
export function CodeBlock(props: CodeBlockProps): ReactElement {
  const block = (
    <pre className={classOf(props)}>
      <code>{props.text}</code>
    </pre>
  );
  if (props.label === undefined) {
    return block;
  }
  return (
    <figure className="code-block">
      <figcaption>
        <span>{props.label}</span>
        <CopyButton text={props.text} secret={props.secret === true} />
      </figcaption>
      {block}
    </figure>
  );
}

export interface FactsProps {
  readonly children: ReactNode;
}

/** A list of named values. */
export function Facts({ children }: FactsProps): ReactElement {
  return <dl className="facts">{children}</dl>;
}

export interface FactProps {
  readonly name: string;
  readonly children: ReactNode;
}

/** One named value of a Facts list. */
export function Fact({ name, children }: FactProps): ReactElement {
  return (
    <div className="fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  );
}
