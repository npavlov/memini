import type { ReactElement, ReactNode } from "react";

import { errorAdvice, errorReference } from "../ipc/app-error";
import type { AppError } from "../ipc/app-error";
import type { Tone } from "./tone";

export interface NoticeProps {
  readonly tone: Tone;
  readonly title?: string;
  readonly children?: ReactNode;
}

export function Notice({ tone, title, children }: NoticeProps): ReactElement {
  return (
    <div className={`notice notice-${tone}`} role={tone === "bad" ? "alert" : "status"}>
      {title === undefined ? null : <p className="notice-title">{title}</p>}
      {children}
    </div>
  );
}

export interface ErrorNoticeProps {
  readonly error: AppError | null;
}

/** An error of a command, as text: the message, what to do about it, and how to find it in a log. */
export function ErrorNotice({ error }: ErrorNoticeProps): ReactElement | null {
  if (error === null) {
    return null;
  }
  const advice = errorAdvice(error);
  const reference = errorReference(error);
  return (
    <Notice tone="bad" title={error.message}>
      {advice === null ? null : <p>{advice}</p>}
      {reference === null ? null : <p className="muted small">{reference}</p>}
    </Notice>
  );
}
