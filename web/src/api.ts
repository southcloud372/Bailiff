// 后端 API 客户端。开发态经 Vite 代理（/v1、/healthz → 127.0.0.1:8000），同源无 CORS。

const BASE = "";

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(BASE + path, init);
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // 非 JSON 错误体，保留状态码文案
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export interface ChatReply {
  reply: string;
}

export function healthz(): Promise<{ status: string }> {
  return request("/healthz");
}

export function listTools(token: string): Promise<{ tools: string[] }> {
  return request("/v1/tools", {
    headers: { Authorization: `Bearer ${token}` },
  });
}

export function chat(token: string, message: string): Promise<ChatReply> {
  return request("/v1/chat", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}
