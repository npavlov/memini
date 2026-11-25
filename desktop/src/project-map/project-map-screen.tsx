import type { ReactElement } from "react";

import { Notice } from "../ui/notice";
import { Screen } from "../ui/panel";

/** A placeholder: the catalog this screen will show has no API yet. */
export function ProjectMapScreen(): ReactElement {
  return (
    <Screen
      title="Project map"
      lead="A project’s domains, subdomains and services, and the links between them."
    >
      <Notice tone="info" title="Not available yet">
        <p>
          The map is drawn from the server’s catalog, which this version of the server does not
          serve. This screen will show the catalog and store corrections once it does.
        </p>
      </Notice>
    </Screen>
  );
}
