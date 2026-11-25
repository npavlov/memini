import { describe, expect, it } from "vitest";

import { ShapeError } from "../json/read-json";
import {
  activeConnection,
  changesOrigin,
  checkConnectionForm,
  dropsToken,
  isLoopbackAddress,
  isPlainHttpToAnotherComputer,
  localConnection,
  newForm,
  parseConnectionsView,
  toConnectionInput,
} from "./connections";
import type { Connection, ConnectionForm } from "./connections";

const remote: Connection = {
  id: "c1",
  name: "Harbour team",
  kind: "remote",
  url: "https://memini.example.org",
  allowHttp: false,
  hasToken: true,
};

const local: Connection = {
  id: "c2",
  name: "This computer",
  kind: "local",
  url: "http://127.0.0.1:6364",
  allowHttp: false,
  hasToken: false,
};

function form(changes: Partial<ConnectionForm>): ConnectionForm {
  return { id: null, name: "Harbour team", kind: "remote", url: remote.url, allowHttp: false, ...changes };
}

describe("parseConnectionsView", () => {
  it("reads the saved connections and the active one from the answer of the Rust side", () => {
    const view = parseConnectionsView({
      active_id: "c1",
      connections: [
        {
          id: "c1",
          name: "Harbour team",
          kind: "remote",
          url: "https://memini.example.org",
          allow_http: false,
          has_token: true,
        },
      ],
    });

    expect(view).toEqual({ activeId: "c1", connections: [remote] });
  });

  it("throws a ShapeError when a connection has no id", () => {
    const parse = (): unknown =>
      parseConnectionsView({ connections: [{ url: "https://memini.example.org" }] });

    expect(parse).toThrow(ShapeError);
  });

  it("reads an answer without a list as no connections", () => {
    expect(parseConnectionsView({})).toEqual({ activeId: null, connections: [] });
  });
});

describe("activeConnection and localConnection", () => {
  const view = { activeId: "c1", connections: [remote, local] };

  it("find the connection the view marks active", () => {
    expect(activeConnection(view)).toBe(remote);
  });

  it("return null when the active id names no connection", () => {
    expect(activeConnection({ ...view, activeId: "gone" })).toBeNull();
  });

  it("find the connection of this computer", () => {
    expect(localConnection(view)).toBe(local);
  });
});

describe("isLoopbackAddress", () => {
  it.each([
    ["http://127.0.0.1:6364", true],
    ["http://localhost:7000", true],
    ["http://[::1]:6364", true],
    ["http://127.9.9.9", true],
    ["https://memini.example.org", false],
    ["https://127.0.0.1.example.org", false],
    ["http://localhost.example.org", false],
    ["not an address", false],
  ])("tells whether %s names this computer", (address, expected) => {
    expect(isLoopbackAddress(address)).toBe(expected);
  });
});

describe("isPlainHttpToAnotherComputer", () => {
  it.each([
    ["http://memini.example.org:6364", true],
    ["https://memini.example.org", false],
    ["http://127.0.0.1:6364", false],
    ["", false],
  ])("tells whether a token sent to %s could be read on the network", (address, expected) => {
    expect(isPlainHttpToAnotherComputer(address)).toBe(expected);
  });
});

describe("checkConnectionForm", () => {
  it("accepts a named remote connection over https", () => {
    expect(checkConnectionForm(form({}))).toBeNull();
  });

  it("accepts plain http to another computer once the user allowed it", () => {
    const allowed = form({ url: "http://memini.example.org:6364", allowHttp: true });

    expect(checkConnectionForm(allowed)).toBeNull();
  });

  it.each([
    ["has no name", form({ name: "  " }), "name"],
    ["has no scheme", form({ url: "memini.example.org" }), "address"],
    ["uses another scheme", form({ url: "ftp://memini.example.org" }), "address"],
    ["holds credentials", form({ url: "https://sam:pw@memini.example.org" }), "user name"],
    ["has a query", form({ url: "https://memini.example.org/?a=b" }), "query"],
    ["is local but names another computer", form({ kind: "local" }), "this computer"],
    ["sends the token over plain http", form({ url: "http://memini.example.org" }), "Plain HTTP"],
  ])("reports the problem when the form %s", (_what, invalid, word) => {
    expect(checkConnectionForm(invalid)).toContain(word);
  });
});

describe("newForm", () => {
  it("starts a local connection at the address a local server answers on", () => {
    expect(newForm("local", local.url)).toMatchObject({ id: null, kind: "local", url: local.url });
  });

  it("starts a remote connection without a name", () => {
    expect(newForm("remote", local.url)).toMatchObject({ kind: "remote", name: "" });
  });
});

describe("toConnectionInput", () => {
  it("trims the name and the address and keeps the id", () => {
    const input = toConnectionInput(form({ id: "c1", name: " Harbour ", url: ` ${remote.url} ` }));

    expect(input).toEqual({
      id: "c1",
      name: "Harbour",
      kind: "remote",
      url: remote.url,
      allow_http: false,
    });
  });

  it("never asks for plain http on a local connection", () => {
    const input = toConnectionInput(form({ kind: "local", url: local.url, allowHttp: true }));

    expect(input["allow_http"]).toBe(false);
  });
});

describe("changesOrigin", () => {
  it.each([
    ["the host changes", "https://other.example.org", true],
    ["the port changes", "https://memini.example.org:8443", true],
    ["the scheme changes", "http://memini.example.org", true],
    ["only a trailing slash is added", "https://memini.example.org/", false],
    ["only a path prefix is added", "https://memini.example.org/prefix", false],
  ])("tells whether the token is dropped when %s", (_what, url, expected) => {
    expect(changesOrigin(form({ id: "c1", url }), remote)).toBe(expected);
  });

  it("returns false for a connection that is not saved yet", () => {
    expect(changesOrigin(form({}), null)).toBe(false);
  });
});

describe("dropsToken", () => {
  it("returns true when a connection with a token moves to another server", () => {
    expect(dropsToken(form({ id: "c1", url: "https://other.example.org" }), remote)).toBe(true);
  });

  it("returns false when the address stays the same server", () => {
    expect(dropsToken(form({ id: "c1", url: `${remote.url}/prefix` }), remote)).toBe(false);
  });

  it("returns false when the connection has no token to lose", () => {
    const moved = form({ id: "c2", kind: "local", url: "https://other.example.org" });

    expect(dropsToken(moved, local)).toBe(false);
  });

  it("returns false when the local connection moves to another port of this computer", () => {
    const withToken = { ...local, hasToken: true };
    const moved = form({ id: "c2", kind: "local", url: "http://127.0.0.1:7001" });

    expect(dropsToken(moved, withToken)).toBe(false);
  });

  it("returns true when a remote connection on this computer moves to another port", () => {
    const second = { ...remote, url: "http://127.0.0.1:7000" };
    const moved = form({ id: "c1", url: "http://127.0.0.1:7001" });

    expect(dropsToken(moved, second)).toBe(true);
  });
});
