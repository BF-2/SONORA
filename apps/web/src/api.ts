import type { Bag } from "./types";
let endpoint = "http://127.0.0.1:8765/api/v1";
let token = sessionStorage.getItem("sonora-token") || "";
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("token")) {
  token = fragment.get("token") || "";
  sessionStorage.setItem("sonora-token", token);
  history.replaceState({}, "", location.pathname + location.search);
}
export function setConnection(value: string, base?: string) {
  token = value.trim();
  sessionStorage.setItem("sonora-token", token);
  if (base) {
    const url = new URL(base);
    if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
      throw new Error("此版本只连接明确配置的本机服务");
    endpoint = base.replace(/\/$/, "");
    if (!endpoint.endsWith("/api/v1")) endpoint += "/api/v1";
  }
}
export async function initConnection() {
  if ("__TAURI_INTERNALS__" in window) {
    const { invoke } = await import("@tauri-apps/api/core");
    const c = await invoke<Bag>("get_connection");
    setConnection(c.token, c.endpoint || c.base_url || c.url);
  }
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function request<T = Bag>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Authorization", `Bearer ${token}`);
  if (options.body && !(options.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const res = await fetch(endpoint + path, { ...options, headers });
  if (!res.ok) {
    let message = `请求失败 (${res.status})`;
    try {
      const b = await res.json();
      message =
        typeof b.detail === "string"
          ? b.detail
          : b.detail?.message || b.message || JSON.stringify(b.detail || b);
    } catch {}
    throw new ApiError(message, res.status);
  }
  return res.json();
}
export const post = <T = Bag>(path: string, body: unknown = {}) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });
export const patch = <T = Bag>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });
export async function media(path: string) {
  const res = await fetch(endpoint + path, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!res.ok) throw new ApiError(`读取文件失败 (${res.status})`, res.status);
  return res.blob();
}
export async function download(path: string, name: string) {
  const blob = await media(path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
export function jsonDownload(data: unknown, name: string) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
