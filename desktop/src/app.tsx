import { useCallback, useState } from "react";
import type { ReactElement } from "react";

import { AppFrame, EmptyState, ErrorNotice, NavItem, Sidebar, Splash, Text } from "./components";
import { tokenProblem } from "./connections/connection-check";
import type { TokenProblem } from "./connections/connection-check";
import { activeConnection } from "./connections/connections";
import type { Connection, ConnectionsView } from "./connections/connections";
import { listConnections } from "./connections/connections-api";
import { ConnectionsScreen } from "./connections/connections-screen";
import { TokenNeeded } from "./connections/token-needed";
import { DataScreen } from "./data/data-screen";
import { useLoad } from "./hooks/use-load";
import { readAppInfo } from "./ipc/app-info";
import type { AppInfo } from "./ipc/app-info";
import type { Backend } from "./ipc/backend";
import { useBackend } from "./ipc/backend-context";
import { McpScreen } from "./mcp/mcp-screen";
import { ProjectMapScreen } from "./project-map/project-map-screen";
import { ServerScreen } from "./server/server-screen";
import { StatusBar } from "./status/status-bar";
import { useConnectionHealth } from "./status/use-connection-health";
import { RepositoriesScreen } from "./repositories/repositories-screen";
import { TokensScreen } from "./tokens/tokens-screen";

/** The screens of the navigation. The state of the connection is not one: it is the bar below. */
const SCREENS = [
  { id: "connection", label: "Connection" },
  { id: "server", label: "Server" },
  { id: "mcp", label: "MCP" },
  { id: "tokens", label: "Tokens" },
  { id: "repos", label: "Codebases" },
  { id: "data", label: "Data" },
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
    return (
      <Splash>
        <Text tone="muted">Starting…</Text>
      </Splash>
    );
  }
  if (boot.state.phase === "failed") {
    return (
      <Splash>
        <ErrorNotice error={boot.state.error} />
      </Splash>
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
    boot.view.connections.length === 0 ? "connection" : "data",
  );
  const sidebar = (
    <Sidebar footer={boot.info.version === "" ? "" : `Version ${boot.info.version}`}>
      {SCREENS.map((item) => (
        <NavItem
          key={item.id}
          current={item.id === screen}
          onSelect={() => {
            setScreen(item.id);
          }}
        >
          {item.label}
        </NavItem>
      ))}
    </Sidebar>
  );
  const active = activeConnection(view);
  const watch = useConnectionHealth(active);
  // Known at once for a connection without a token; for a refused one, once the server said so.
  const problem = active === null ? null : tokenProblem(active, watch.health?.token ?? null);
  return (
    <AppFrame sidebar={sidebar} statusBar={<StatusBar connection={active} watch={watch} />}>
      <CurrentScreen
        screen={screen}
        view={view}
        info={boot.info}
        onView={setView}
        token={{
          problem,
          onAddToken: () => {
            setScreen("connection");
          },
        }}
      />
    </AppFrame>
  );
}

/** What keeps the selected connection from its data, and the way to the token. */
interface TokenGate {
  readonly problem: TokenProblem | null;
  readonly onAddToken: () => void;
}

interface CurrentScreenProps {
  readonly screen: ScreenId;
  readonly view: ConnectionsView;
  readonly info: AppInfo;
  readonly onView: (view: ConnectionsView) => void;
  readonly token: TokenGate;
}

function CurrentScreen({ screen, view, info, onView, token }: CurrentScreenProps): ReactElement {
  const active = activeConnection(view);
  switch (screen) {
    case "connection":
      return <ConnectionsScreen view={view} info={info} onView={onView} />;
    case "server":
      return <ServerScreen view={view} info={info} onView={onView} />;
    case "mcp":
    case "tokens":
    case "repos":
    case "data":
    case "map":
      return active === null ? (
        <EmptyState title="No connection is selected">
          <Text>Add a server on the Connection screen, or choose one of the saved connections.</Text>
        </EmptyState>
      ) : (
        <ConnectionScreen screen={screen} connection={active} info={info} token={token} />
      );
  }
}

interface ConnectionScreenProps {
  readonly screen: "mcp" | "tokens" | "repos" | "data" | "map";
  readonly connection: Connection;
  readonly info: AppInfo;
  readonly token: TokenGate;
}

/** A screen about one connection. It is keyed by the connection, so answers never cross over. */
function ConnectionScreen({ screen, connection, info, token }: ConnectionScreenProps): ReactElement {
  const key = `${connection.id}:${connection.url}:${String(connection.hasToken)}`;
  const { problem, onAddToken } = token;
  // The MCP screen has an address to show without a token; the others have only the server's data.
  if (problem !== null && screen !== "mcp") {
    const title = SCREENS.find((item) => item.id === screen)?.label ?? "";
    return <TokenNeeded title={title} problem={problem} onAddToken={onAddToken} />;
  }
  switch (screen) {
    case "mcp":
      return <McpScreen key={key} connection={connection} token={token} />;
    case "tokens":
      return <TokensScreen key={key} connection={connection} />;
    case "repos":
      return <RepositoriesScreen key={key} connection={connection} info={info} />;
    case "data":
      return <DataScreen key={key} connection={connection} />;
    case "map":
      return <ProjectMapScreen key={key} connection={connection} />;
  }
}
