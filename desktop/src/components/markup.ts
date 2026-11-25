// For the tests of the library: the HTML a component renders, without a browser.

import { createElement } from "react";
import type { ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import { BackendProvider } from "../ipc/backend-context";
import { createFakeBackend } from "../ipc/fake-backend";

/** The markup of an element as it is first rendered, inside a backend that answers nothing. */
export function markupOf(element: ReactElement): string {
  const backend = createFakeBackend({ answers: {} });
  return renderToStaticMarkup(createElement(BackendProvider, { backend, children: element }));
}
