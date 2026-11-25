import type { ReactElement, ReactNode } from "react";

export interface StatusBarFrameProps {
  /** What opens above the bar; null while it is closed. */
  readonly details?: ReactNode;
  /** The id of the details, for the button that opens them. */
  readonly detailsId?: string;
  /** The bar's own line: a name, chips, StatusBarActions. */
  readonly children: ReactNode;
}

/**
 * The bar at the bottom of the window. It is always visible; its details open above it, over
 * the screen, and scroll on their own.
 */
export function StatusBarFrame({ details, detailsId, children }: StatusBarFrameProps): ReactElement {
  return (
    <footer className="statusbar">
      {details === undefined || details === null ? null : (
        <div className="statusbar-details" id={detailsId} role="region" aria-label="Status details">
          {details}
        </div>
      )}
      {children}
    </footer>
  );
}

export interface StatusBarActionsProps {
  readonly children: ReactNode;
}

/** The buttons of the bar, at its far end. */
export function StatusBarActions({ children }: StatusBarActionsProps): ReactElement {
  return <span className="statusbar-actions">{children}</span>;
}
