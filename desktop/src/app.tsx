import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { activeConnection } from "./connections/connections";
import type { Connection, ConnectionsView } from "./connections/connections";
import { listConnections } from "./connections/connections-api";
import { ConnectionsScreen } from "./connections/connections-screen";
import { readAppInfo } from "./ipc/app-info";
import type { AppInfo } from "./ipc/app-info";
import type { Backend } from "./ipc/backend";
import { useBackend } from "./ipc/backend-context";
import { McpScreen } from "./mcp/mcp-screen";
import { ProjectMapScreen } from "./project-map/project-map-screen";
import { ServerScreen } from "./server/server-screen";
import { StatusScreen } from "./status/status-screen";
import { TokensScreen } from "./tokens/tokens-screen";
import { ErrorNotice, Notice } from "./ui/notice";
import { Pill } from "./ui/pill";
import { useLoad } from "./ui/use-load";

const SCREENS = [
  { id: "connection", label: "Connection" },
  { id: "server", label: "Server" },
  { id: "mcp", label: "MCP" },
  { id: "tokens", label: "Tokens" },
  { id: "status", label: "Status" },
  { id: "map", label: "Project map" },
] as const;

type ScreenId = (typeof SCREENS)[number]["id"];

interface Boot {
  readonly info: AppInfo;
  readonly view: ConnectionsView;
}

async function loadBoot(backend: Backend): Promise<Boot> {
  const [info, view] = await Promise.all([readAppInfo(backend), listConnections(backend)]);
  return { info, view };
}

/** The whole app. What it talks to comes from the BackendProvider around it. */
export function App(): ReactElement {
  const backend = useBackend();
  const boot = useLoad(useCallback(() => loadBoot(backend), [backend]));
  if (boot.state.phase === "loading") {
    return <p className="boot muted">Starting…</p>;
  }
  if (boot.state.phase === "failed") {
    return (
      <div className="boot">
        <ErrorNotice error={boot.state.error} />
      </div>
    );
  }
  return <Shell boot={boot.state.data} />;
}

interface ShellProps {
  readonly boot: Boot;
}

function Shell({ boot }: ShellProps): ReactElement {
  const [view, setView] = useState(boot.view);
  const [screen, setScreen] = useState<ScreenId>(
    boot.view.connections.length === 0 ? "connection" : "status",
  );
  const active = activeConnection(view);
  return (
    <div className="shell">
      <nav className="sidebar" aria-label="Screens">
        <p className="brand">Memini</p>
        {SCREENS.map((item) => (
          <button
            key={item.id}
            type="button"
            className={item.id === screen ? "nav-item current" : "nav-item"}
            aria-current={item.id === screen ? "page" : undefined}
            onClick={() => {
              setScreen(item.id);
            }}
          >
            {item.label}
          </button>
        ))}
        <p className="version muted small">
          {boot.info.version === "" ? "" : `Version ${boot.info.version}`}
        </p>
      </nav>
      <div className="content">
        <ActiveConnectionBar connection={active} />
        <main>
          <CurrentScreen screen={screen} view={view} info={boot.info} onView={setView} />
        </main>
      </div>
    </div>
  );
}

interface ActiveConnectionBarProps {
  readonly connection: Connection | null;
}

function ActiveConnectionBar({ connection }: ActiveConnectionBarProps): ReactElement {
  if (connection === null) {
    return <header className="bar muted">No connection is active.</header>;
  }
  return (
    <header className="bar">
      <strong>{connection.name}</strong>
      <span className="muted">{connection.url}</span>
      <Pill tone="info">{connection.kind === "local" ? "this computer" : "remote"}</Pill>
      <Pill tone={connection.hasToken ? "good" : "warn"}>
        {connection.hasToken ? "token in the keychain" : "no token"}
      </Pill>
    </header>
  );
}

interface CurrentScreenProps {
  readonly screen: ScreenId;
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
}

function CurrentScreen({ screen, view, info, onView }: CurrentScreenProps): ReactElement {
  const active = activeConnection(view);
  switch (screen) {
    case "connection":
      return <ConnectionsScreen view={view} info={info} onView={onView} />;
    case "server":
      return <ServerScreen view={view} info={info} onView={onView} />;
    case "map":
      return <ProjectMapScreen />;
    case "mcp":
    case "tokens":
    case "status":
      return active === null ? (
        <Notice tone="info" title="No connection is active">
          <p>Add a server on the Connection screen, or choose one of the saved connections.</p>
        </Notice>
      ) : (
        <ConnectionScreen screen={screen} connection={active} />
      );
  }
}

interface ConnectionScreenProps {
  readonly screen: "mcp" | "tokens" | "status";
  readonly connection: Connection;
}

/** A screen about one connection. It is keyed by the connection, so answers never cross over. */
function ConnectionScreen({ screen, connection }: ConnectionScreenProps): ReactElement {
  const key = `${connection.id}:${connection.url}:${String(connection.hasToken)}`;
  switch (screen) {
    case "mcp":
      return <McpScreen key={key} connection={connection} />;
    case "tokens":
      return <TokensScreen key={key} connection={connection} />;
    case "status":
      return <StatusScreen key={key} connection={connection} />;
  }
}
