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

// SSE 流式对话：逐 token 回调 onDelta，直到 [DONE] 或 error 事件。
// 用 fetch + ReadableStream（而非 EventSource）以便携带 Authorization 头。
export async function chatStream(
  token: string,
  message: string,
  onDelta: (delta: string) => void,
): Promise<void> {
  const res = await fetch(BASE + "/v1/chat/stream", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
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
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      const s = line.trim();
      if (!s.startsWith("data:")) continue;
      const payload = s.slice(5).trim();
      if (payload === "[DONE]") return;
      const parsed = JSON.parse(payload) as { delta?: string; error?: string };
      if (parsed.error) throw new Error(parsed.error);
      if (parsed.delta) onDelta(parsed.delta);
    }
  }
}
