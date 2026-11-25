import type { ReactElement, ReactNode } from "react";

/** Muted is secondary text; hint asks the user for something; failure reports what went wrong. */
export type TextTone = "plain" | "muted" | "hint" | "failure";

export interface TextProps {
  readonly tone?: TextTone;
  readonly small?: boolean;
  /** Sets the text inside a line of other text, instead of as a paragraph of its own. */
  readonly inline?: boolean;
  readonly children: ReactNode;
}

/** A paragraph, or with `inline` a run of text, in one of the app's text styles. */
export function Text({ tone = "plain", small = false, inline = false, children }: TextProps): ReactElement {
  const name = `text text-${tone}${small ? " text-small" : ""}`;
  return inline ? <span className={name}>{children}</span> : <p className={name}>{children}</p>;
}

export interface FormProps {
  /** Called when the form is submitted, by its submit button or the Enter key. */
  readonly onSubmit: () => unknown;
  /** Lays a field and its button out in one row. */
  readonly inline?: boolean;
  readonly children: ReactNode;
}

/** A form. It never navigates: submitting calls `onSubmit` and nothing else. */
export function Form({ onSubmit, inline = false, children }: FormProps): ReactElement {
  return (
    <form
      className={inline ? "form form-inline" : "form"}
      onSubmit={(event) => {
        event.preventDefault();
        return onSubmit();
      }}
    >
      {children}
    </form>
  );
}

export interface LayoutProps {
  readonly children: ReactNode;
}

/** Things one under the other, with the app's spacing. */
export function Stack({ children }: LayoutProps): ReactElement {
  return <div className="stack">{children}</div>;
}

/** Two fields side by side. */
export function Columns({ children }: LayoutProps): ReactElement {
  return <div className="columns">{children}</div>;
}

/** Many fields in as many columns as fit. */
export function FieldGrid({ children }: LayoutProps): ReactElement {
  return <div className="field-grid">{children}</div>;
}

/** A short value that must stay on one line, such as a duration in a table. */
export function NoWrap({ children }: LayoutProps): ReactElement {
  return <span className="nowrap">{children}</span>;
}

export interface AppFrameProps {
  /** The Sidebar. */
  readonly sidebar: ReactNode;
  /** The bar at the bottom of the window. */
  readonly statusBar: ReactNode;
  /** The current screen. */
  readonly children: ReactNode;
}

/** The window: the navigation at the side, the screen beside it, the status bar across the bottom. */
export function AppFrame({ sidebar, statusBar, children }: AppFrameProps): ReactElement {
  return (
    <div className="app-frame">
      {sidebar}
      <main>{children}</main>
      {statusBar}
    </div>
  );
}

/** What the window shows before the app has loaded: one message in the middle. */
export function Splash({ children }: LayoutProps): ReactElement {
  return <div className="splash">{children}</div>;
}
