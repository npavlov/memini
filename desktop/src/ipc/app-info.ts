import { readObject, readString } from "../json/read-json";
import type { Backend } from "./backend";

export interface AppInfo {
  readonly version: string;
  /** `macos`, `windows` or `linux`. */
  readonly os: string;
  /** Where a local server answers unless the launcher says otherwise. */
  readonly defaultLocalUrl: string;
}

export const UNKNOWN_APP: AppInfo = {
  version: "",
  os: "",
  defaultLocalUrl: "http://127.0.0.1:6364",
};

export async function readAppInfo(backend: Backend): Promise<AppInfo> {
  const info = readObject(await backend.invoke("app_info"), "the app's description");
  return {
    version: readString(info["version"]) ?? UNKNOWN_APP.version,
    os: readString(info["os"]) ?? UNKNOWN_APP.os,
    defaultLocalUrl: readString(info["default_local_url"]) ?? UNKNOWN_APP.defaultLocalUrl,
  };
}
