import { useCallback } from "react";
import type { ReactElement } from "react";

import { Button, ErrorNotice, Mono, Notice, Text } from "../components";
import { formOf, localConnection, newForm } from "../connections/connections";
import type { ConnectionForm, ConnectionsView } from "../connections/connections";
import { saveConnection } from "../connections/connections-api";
import { useAction } from "../hooks/use-action";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import type { LauncherStatus } from "./launcher";
import { movedLocalAddress, reportedLocalAddress } from "./local-address";

export interface LocalConnectionProps {
  readonly status: LauncherStatus;
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
}

/**
 * The connection for the server on this computer. The launcher reports where the server answers;
 * the app offers that address and the user decides: nothing is changed without a click, and only
 * an address on this computer is offered.
 */
export function LocalConnection(props: LocalConnectionProps): ReactElement | null {
  const { status, view, info, onView } = props;
  const backend = useBackend();
  const save = useCallback(
    async (form: ConnectionForm): Promise<void> => {
      onView(await saveConnection(backend, form));
    },
    [backend, onView],
  );
  const saving = useAction(save);
  const saved = localConnection(view);
  if (saved === null) {
    const form = newForm("local", reportedLocalAddress(status) ?? info.defaultLocalUrl);
    return (
      <Notice tone="info" title="This computer is not a saved connection yet">
        <Text>
          Save it to see its status and manage its tokens here. Its address will be{" "}
          <Mono>{form.url}</Mono>.
        </Text>
        <Button busy={saving.pending ? "Adding…" : null} onClick={() => saving.run(form)}>
          Add this computer as a connection
        </Button>
        <ErrorNotice error={saving.error} />
      </Notice>
    );
  }
  const moved = movedLocalAddress(saved, status);
  if (moved === null) {
    return null;
  }
  return (
    <Notice tone="warn" title="The saved connection uses another address">
      <Text>
        The launcher reports <Mono>{moved}</Mono>; the connection “{saved.name}” uses{" "}
        <Mono>{saved.url}</Mono>. The server can come back on another port, for example after its
        MCP endpoint was switched. The token is kept: it is the same server on this computer.
      </Text>
      <Button busy={saving.pending ? "Saving…" : null} onClick={() => saving.run({ ...formOf(saved), url: moved })}>
        Use the launcher’s address
      </Button>
      <ErrorNotice error={saving.error} />
    </Notice>
  );
}
