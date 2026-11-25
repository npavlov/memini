import type { ReactElement, ReactNode } from "react";

import { Text } from "./layout";

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
        <Text tone="muted">{lead}</Text>
      </header>
      {children}
    </div>
  );
}

export interface PanelProps {
  readonly title: string;
  /** Buttons or a chip that belong to the whole panel, shown beside its title. */
  readonly actions?: ReactNode;
  readonly children: ReactNode;
}

/** A titled surface that holds one subject of a screen. */
export function Panel({ title, actions, children }: PanelProps): ReactElement {
  return (
    <section className="panel">
      <header className="panel-header">
        <h2>{title}</h2>
        {actions === undefined ? null : <div className="actions">{actions}</div>}
      </header>
      {children}
    </section>
  );
}

export interface SectionProps {
  readonly title: string;
  /** What stands beside the title: counts, a chip. */
  readonly aside?: ReactNode;
  readonly children: ReactNode;
}

/** A part of a panel with a heading of its own. */
export function Section({ title, aside, children }: SectionProps): ReactElement {
  return (
    <section className="section">
      <header className="section-header">
        <h3>{title}</h3>
        {aside}
      </header>
      {children}
    </section>
  );
}

export interface CardProps {
  readonly title?: string;
  readonly subtitle?: string;
  readonly children?: ReactNode;
}

/** One item of a list of results: a title, a line under it, and whatever the item holds. */
export function Card({ title, subtitle, children }: CardProps): ReactElement {
  return (
    <article className="card">
      {title === undefined || title === "" ? null : <p className="card-title">{title}</p>}
      {subtitle === undefined || subtitle === "" ? null : <Text tone="muted" small>{subtitle}</Text>}
      {children}
    </article>
  );
}

export interface EmptyStateProps {
  readonly title: string;
  /** What to do so that there is something to show. */
  readonly children?: ReactNode;
}

/** What stands where a list or a screen has nothing to show yet. */
export function EmptyState({ title, children }: EmptyStateProps): ReactElement {
  return (
    <div className="empty-state" role="status">
      <p className="empty-state-title">{title}</p>
      {children}
    </div>
  );
}

export interface ActionsProps {
  readonly children: ReactNode;
}

/** A row of buttons, and of what belongs beside them. */
export function Actions({ children }: ActionsProps): ReactElement {
  return <div className="actions">{children}</div>;
}

export interface MonoProps {
  readonly children: ReactNode;
}

/** An address, a path or another literal value, in the mono font. */
export function Mono({ children }: MonoProps): ReactElement {
  return <span className="mono">{children}</span>;
}
