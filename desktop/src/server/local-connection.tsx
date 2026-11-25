import { useCallback } from "react";
import type { ReactElement } from "react";

import { formOf, localConnection, newForm } from "../connections/connections";
import type { ConnectionForm, ConnectionsView } from "../connections/connections";
import { saveConnection } from "../connections/connections-api";
import type { AppInfo } from "../ipc/app-info";
import { useBackend } from "../ipc/backend-context";
import { ErrorNotice, Notice } from "../ui/notice";
import { useAction } from "../ui/use-action";
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
        <p>Save it to see its status and manage its tokens here. Its address will be {form.url}.</p>
        <button type="button" className="button" disabled={saving.pending} onClick={() => saving.run(form)}>
          Add this computer as a connection
        </button>
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
      <p>
        The launcher reports {moved}; the connection “{saved.name}” uses {saved.url}. The server
        can come back on another port, for example after its MCP endpoint was switched. The token
        is kept: it is the same server on this computer.
      </p>
      <button
        type="button"
        className="button"
        disabled={saving.pending}
        onClick={() => saving.run({ ...formOf(saved), url: moved })}
      >
        Use the launcher’s address
      </button>
      <ErrorNotice error={saving.error} />
    </Notice>
  );
}
