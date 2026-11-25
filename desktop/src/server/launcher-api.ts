// The launcher commands of the Rust side. The front end names a change; it never passes an
// argument for the program.

import type { Backend } from "../ipc/backend";
import { parseLauncherInfo, parseLauncherResult, parseLauncherStatus } from "./launcher";
import type { LauncherChange, LauncherInfo, LauncherResult, LauncherStatus } from "./launcher";

export async function readLauncherInfo(backend: Backend): Promise<LauncherInfo> {
  return parseLauncherInfo(await backend.invoke("launcher_info"));
}

export async function setLauncherPath(backend: Backend, path: string | null): Promise<LauncherInfo> {
  return parseLauncherInfo(await backend.invoke("launcher_set_path", { path }));
}

export async function readLauncherStatus(backend: Backend): Promise<LauncherStatus> {
  return parseLauncherStatus(await backend.invoke("launcher_status"));
}

export async function changeServer(
  backend: Backend,
  change: LauncherChange,
): Promise<LauncherResult> {
  return parseLauncherResult(await backend.invoke("launcher_change", { change }));
}
