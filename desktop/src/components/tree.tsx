import type { ReactElement, ReactNode } from "react";

export interface TreeProps {
  /** What the tree is of, for assistive technology. */
  readonly label: string;
  /** `TreeItem`s. */
  readonly children: ReactNode;
}

/** Things inside things, such as the domains of a project with their subdomains and code. */
export function Tree({ label, children }: TreeProps): ReactElement {
  return (
    <ul className="tree" aria-label={label}>
      {children}
    </ul>
  );
}

export interface TreeItemProps {
  /** What the item is, in a word: "Domain", "Service". */
  readonly kind: string;
  readonly title: ReactNode;
  /** At the end of the item's line: a count, a state. */
  readonly aside?: ReactNode;
  /** Under the line: a description. */
  readonly detail?: ReactNode;
  /** The `TreeItem`s inside this one. */
  readonly children?: ReactNode;
}

/** One item of a `Tree`: its kind and title on a line, and the items inside it one level in. */
export function TreeItem({ kind, title, aside, detail, children }: TreeItemProps): ReactElement {
  return (
    <li className="tree-item">
      <div className="tree-line">
        <span className="tree-kind">{kind}</span>
        <span className="tree-title">{title}</span>
        {aside === undefined ? null : <span className="tree-aside">{aside}</span>}
      </div>
      {detail === undefined ? null : <div className="tree-detail">{detail}</div>}
      {children === undefined ? null : <ul className="tree tree-inside">{children}</ul>}
    </li>
  );
}
