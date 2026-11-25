import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import type { AppError } from "../ipc/app-error";
import { useBackend } from "../ipc/backend-context";
import { ConfirmButton } from "../ui/confirm-button";
import { ErrorNotice, Notice } from "../ui/notice";
import { Panel } from "../ui/panel";
import { Pill } from "../ui/pill";
import { useAction } from "../ui/use-action";
import type { Action } from "../ui/use-action";
import type { Connection, ConnectionsView } from "./connections";
import { clearToken, takeTokenFromClipboard, takeTokenFromFile } from "./connections-api";

export interface ConnectionTokenProps {
  readonly connection: Connection;
  readonly onView: (view: ConnectionsView) => void;
}

const LOCAL_HELP =
  "For the server on this computer, the first admin token comes from the terminal: memini-server cli bootstrap prints it once. Copy it, or save it to a file.";
const REMOTE_HELP =
  "Ask the server’s operator for a token: with the read scope to see status, with the admin scope to manage tokens.";

interface TokenActions {
  readonly pasting: Action<void>;
  readonly reading: Action<string>;
  readonly removing: Action<void>;
  /** What the last successful action did, in words. */
  readonly done: string | null;
  readonly error: AppError | null;
}

function useTokenActions({ connection, onView }: ConnectionTokenProps): TokenActions {
  const backend = useBackend();
  const [done, setDone] = useState<string | null>(null);
  const { id } = connection;
  const fromClipboard = useCallback(async (): Promise<void> => {
    onView(await takeTokenFromClipboard(backend, id));
    setDone("The token is in the system keychain, and the clipboard was cleared.");
  }, [backend, id, onView]);
  const fromFile = useCallback(
    async (path: string): Promise<void> => {
      onView(await takeTokenFromFile(backend, id, path));
      setDone("The token is in the system keychain. The file was not changed.");
    },
    [backend, id, onView],
  );
  const remove = useCallback(async (): Promise<void> => {
    onView(await clearToken(backend, id));
    setDone("The token was removed from the system keychain.");
  }, [backend, id, onView]);
  const pasting = useAction(fromClipboard);
  const reading = useAction(fromFile);
  const removing = useAction(remove);
  return { pasting, reading, removing, done, error: pasting.error ?? reading.error ?? removing.error };
}

/**
 * Stores, replaces and removes the token of a connection. There is no field to type a token
 * into: Rust reads it from the clipboard or from a file, and this window never holds it.
 */
export function ConnectionToken(props: ConnectionTokenProps): ReactElement {
  const { connection } = props;
  const actions = useTokenActions(props);
  return (
    <Panel
      title="Token"
      actions={
        <Pill tone={connection.hasToken ? "good" : "warn"}>
          {connection.hasToken ? "stored in the keychain" : "none stored"}
        </Pill>
      }
    >
      <p className="muted">{connection.kind === "local" ? LOCAL_HELP : REMOTE_HELP}</p>
      <p className="muted">
        The token goes from the clipboard or the file straight to the system keychain. It is never
        shown here and never written to the app’s settings.
      </p>
      <TokenButtons hasToken={connection.hasToken} actions={actions} />
      <TokenFileForm pending={actions.reading.pending} onRead={actions.reading.run} />
      {actions.done === null || actions.error !== null ? null : (
        <Notice tone="good" title={actions.done} />
      )}
      <ErrorNotice error={actions.error} />
    </Panel>
  );
}

interface TokenButtonsProps {
  readonly hasToken: boolean;
  readonly actions: TokenActions;
}

function TokenButtons({ hasToken, actions }: TokenButtonsProps): ReactElement {
  const { pasting, removing } = actions;
  return (
    <div className="actions">
      <button
        type="button"
        className="button primary"
        disabled={pasting.pending}
        onClick={() => pasting.run()}
      >
        {hasToken ? "Replace with the token on the clipboard" : "Take the token from the clipboard"}
      </button>
      {hasToken ? (
        <ConfirmButton
          label="Remove token"
          question="Remove the token from the keychain?"
          confirmLabel="Remove"
          disabled={removing.pending}
          onConfirm={() => removing.run()}
        />
      ) : null}
    </div>
  );
}

interface TokenFileFormProps {
  readonly pending: boolean;
  readonly onRead: (path: string) => Promise<void>;
}

function TokenFileForm({ pending, onRead }: TokenFileFormProps): ReactElement {
  const [path, setPath] = useState("");
  return (
    <form
      className="inline-form"
      onSubmit={(event) => {
        event.preventDefault();
        return path.trim() === "" ? undefined : onRead(path);
      }}
    >
      <label className="field grow">
        <span>Or read it from a token file</span>
        <input
          type="text"
          spellCheck={false}
          autoCapitalize="off"
          autoCorrect="off"
          value={path}
          placeholder="~/.config/memini/admin.token"
          onChange={(event) => {
            setPath(event.target.value);
          }}
        />
      </label>
      <button type="submit" className="button" disabled={pending || path.trim() === ""}>
        Read the file
      </button>
    </form>
  );
}
