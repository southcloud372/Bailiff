import { useEffect, useRef, useState } from "react";
import { chat, healthz, listTools } from "./api";

interface Message {
  role: "user" | "assistant";
  content: string;
}

type Health = "checking" | "ok" | "down";

const TOKEN_KEY = "agent_token";

function loadToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export default function App() {
  const [token, setToken] = useState(loadToken);
  const [health, setHealth] = useState<Health>("checking");
  const [tools, setTools] = useState<string[]>([]);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    healthz()
      .then(() => alive && setHealth("ok"))
      .catch(() => alive && setHealth("down"));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, busy]);

  async function connect() {
    setError(null);
    const t = token.trim();
    if (!t) {
      setError("请先粘贴 token（生成：uv run python scripts/mint_token.py demo agent）");
      return;
    }
    localStorage.setItem(TOKEN_KEY, t);
    try {
      const { tools: names } = await listTools(t);
      setTools(names);
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setError(null);
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text }]);
    try {
      const { reply } = await chat(token.trim(), text);
      setMessages((m) => [...m, { role: "assistant", content: reply }]);
    } catch (e) {
      setMessages((m) => [
        ...m,
        { role: "assistant", content: `⚠️ ${(e as Error).message}` },
      ]);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◈</span>
          <span>Agent Platform</span>
          <span className={`badge ${health}`}>
            {health === "ok" ? "网关在线" : health === "down" ? "网关离线" : "检测中…"}
          </span>
        </div>
        <div className="auth">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="粘贴 JWT token"
          />
          <button onClick={() => void connect()}>连接</button>
        </div>
      </header>

      {error && <div className="banner">{error}</div>}

      <main className="layout">
        <aside className="tools">
          <h2>工具 /v1/tools</h2>
          {tools.length === 0 ? (
            <p className="muted">连接后可查看可调用工具</p>
          ) : (
            <ul>
              {tools.map((t) => (
                <li key={t}>
                  <code>{t}</code>
                </li>
              ))}
            </ul>
          )}
          <p className="hint">
            获取 token：
            <code>uv run python scripts/mint_token.py demo agent</code>
          </p>
        </aside>

        <section className="chat">
          <div className="messages" ref={scrollRef}>
            {messages.length === 0 && (
              <p className="muted">向 Agent 提问，例如「写一个文件，然后读回来」</p>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className="who">{m.role === "user" ? "你" : "Agent"}</div>
                <pre className="content">{m.content}</pre>
              </div>
            ))}
            {busy && (
              <div className="msg assistant">
                <div className="who">Agent</div>
                <div className="content">思考中…</div>
              </div>
            )}
          </div>
          <form
            className="composer"
            onSubmit={(e) => {
              e.preventDefault();
              void send();
            }}
          >
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="输入消息，回车发送"
              disabled={busy}
            />
            <button type="submit" disabled={busy || !input.trim()}>
              发送
            </button>
          </form>
        </section>
      </main>
    </div>
  );
}
