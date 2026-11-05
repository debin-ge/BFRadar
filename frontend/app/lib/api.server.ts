import { data } from "react-router";

// 后端内部页面数据（D60）：只在 Node 端经内网取，浏览器从不直接访问后端
const ORIGIN = process.env.API_ORIGIN ?? "http://127.0.0.1:8000";

/** GET /internal/v1{path}；后端 404 变成页面 404，连不上或出错变成 503 / 502。 */
export async function internal<T>(path: string, signal?: AbortSignal): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${ORIGIN}/internal/v1${path}`, { signal, headers: { accept: "application/json" } });
  } catch (error) {
    if (signal?.aborted) throw error;
    console.error(`后端不可用：${path}`, error);
    throw data(null, { status: 503 });
  }
  if (response.status === 404) throw data(null, { status: 404 });
  if (!response.ok) {
    console.error(`后端返回 ${response.status}：${path}`);
    throw data(null, { status: 502 });
  }
  return (await response.json()) as T;
}
