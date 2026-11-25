import type { ReactElement, ReactNode } from "react";

export interface PanelProps {
  readonly title: string;
  /** Buttons that act on the whole panel, shown beside its title. */
  readonly actions?: ReactNode;
  readonly children: ReactNode;
}

export function Panel({ title, actions, children }: PanelProps): ReactElement {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>{title}</h2>
        {actions === undefined ? null : <div className="panel-actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export interface FactProps {
  readonly name: string;
  readonly children: ReactNode;
}

/** One named value in a list of facts. */
export function Fact({ name, children }: FactProps): ReactElement {
  return (
    <div className="fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export interface ScreenProps {
  readonly title: string;
  readonly lead: string;
  readonly children: ReactNode;
}

/** The frame of a screen: its name, one sentence on what it is for, and its panels. */
export function Screen({ title, lead, children }: ScreenProps): ReactElement {
  return (
    <div className="screen">
      <header className="screen-header">
        <h1>{title}</h1>
        <p className="muted">{lead}</p>
      </header>
      {children}
    </div>
  );
}
