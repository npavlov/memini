import { describe, expect, it } from "vitest";

import type { Connection } from "../connections/connections";
import type { LauncherStatus } from "./launcher";
import { movedLocalAddress, reportedLocalAddress } from "./local-address";

const local: Connection = {
  id: "c1",
  name: "This computer",
  kind: "local",
  url: "http://127.0.0.1:6364",
  allowHttp: false,
  hasToken: true,
};

function launcher(apiUrl: string | null): LauncherStatus {
  return {
    installed: true,
    running: true,
    apiUrl,
    mcpUrl: null,
    mcpEnabled: true,
    image: null,
    directory: null,
    dockerAvailable: true,
    dockerError: null,
    services: [],
  };
}

describe("reportedLocalAddress", () => {
  it("returns the launcher's address when it is on this computer", () => {
    expect(reportedLocalAddress(launcher("http://127.0.0.1:7001"))).toBe("http://127.0.0.1:7001");
  });

  it.each([
    ["names another computer", launcher("https://elsewhere.example.org")],
    ["is absent", launcher(null)],
  ])("returns null when the launcher's address %s", (_what, status) => {
    expect(reportedLocalAddress(status)).toBeNull();
  });

  it("returns null without a launcher", () => {
    expect(reportedLocalAddress(null)).toBeNull();
  });
});

describe("movedLocalAddress", () => {
  it("returns the launcher's address when the port changed", () => {
    expect(movedLocalAddress(local, launcher("http://127.0.0.1:7001"))).toBe("http://127.0.0.1:7001");
  });

  it("returns null when the launcher and the connection agree", () => {
    expect(movedLocalAddress(local, launcher("http://127.0.0.1:6364/"))).toBeNull();
  });

  it("returns null for a remote connection, which the launcher does not manage", () => {
    const remote: Connection = { ...local, kind: "remote", url: "https://memini.example.org" };

    expect(movedLocalAddress(remote, launcher("http://127.0.0.1:7001"))).toBeNull();
  });
});
