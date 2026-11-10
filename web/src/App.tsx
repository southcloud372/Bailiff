import { useCallback, useEffect, useRef, useState } from "react";
import {
  chatStream,
  cost,
  devToken,
  healthz,
  listTools,
  readyz,
  type CostSnapshot,
  type StreamEvent,
} from "./api";

interface ToolCall {
  name: string;
  args: string;
  result?: string;
}

interface Message {
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  tools: ToolCall[];
}

type Health = "checking" | "ok" | "down";

const TOKEN_KEY = "agent_token";

function loadToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export default function App() {
  const [token, setToken] = useState(loadToken);
  const [health, setHealth] = useState<Health>("checking");
  const [model, setModel] = useState<string>("");
  const [tools, setTools] = useState<string[]>([]);
  const [account, setAccount] = useState<CostSnapshot | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // 探针：健康状态与模型后端（mock / deepseek）都在这里读，避免前端猜。
  useEffect(() => {
    let alive = true;
    healthz()
      .then(() => alive && setHealth("ok"))
      .catch(() => alive && setHealth("down"));
    readyz()
      .then((r) => {
        if (!alive) return;
        const llm = r.probes.find((p) => p.name === "llm");
        if (llm) setModel(llm.detail);
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, busy]);

  const refreshAccount = useCallback(async (t: string) => {
    try {
      setAccount(await cost(t));
    } catch {
      // 成本接口失败不影响对话
    }
  }, []);

  const connect = useCallback(
    async (raw?: string) => {
      const t = (raw ?? token).trim();
      setError(null);
      if (!t) {
        setError("还没有 token：点「演示登录」自动获取，或运行 uv run python scripts/mint_token.py demo agent");
        return;
      }
      localStorage.setItem(TOKEN_KEY, t);
      try {
        const { tools: names } = await listTools(t);
        setTools(names);
        void refreshAccount(t);
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [token, refreshAccount],
  );

  // 已有 token 时自动连一次，减少一次点击。
  useEffect(() => {
    if (token) void connect(token);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function demoLogin() {
    setError(null);
    try {
      const { token: t } = await devToken();
      setToken(t);
      await connect(t);
    } catch (e) {
      setError(`演示登录失败（生产环境不提供该端点）：${(e as Error).message}`);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setError(null);
    setBusy(true);
    setMessages((m) => [...m, { role: "user", content: text, reasoning: "", tools: [] }]);
    let started = false;
    const ensure = () => {
      if (started) return;
      started = true;
      setMessages((m) => [...m, { role: "assistant", content: "", reasoning: "", tools: [] }]);
    };
    const patchLast = (fn: (msg: Message) => Message) =>
      setMessages((m) => {
        const next = [...m];
        next[next.length - 1] = fn(next[next.length - 1]);
        return next;
      });

    const handle = (event: StreamEvent) => {
      ensure();
      patchLast((msg) => {
        if (event.type === "delta") return { ...msg, content: msg.content + event.text };
        if (event.type === "reasoning")
          return { ...msg, reasoning: msg.reasoning + event.text };
        if (event.type === "tool_call")
          return { ...msg, tools: [...msg.tools, { name: event.name, args: event.args }] };
        // tool_result 落到最后一次未填结果的同名调用上
        const list = [...msg.tools];
        for (let i = list.length - 1; i >= 0; i -= 1) {
          if (list[i].name === event.name && list[i].result === undefined) {
            list[i] = { ...list[i], result: event.text };
            return { ...msg, tools: list };
          }
        }
        return { ...msg, tools: [...list, { name: event.name, args: "", result: event.text }] };
      });
    };

    try {
      await chatStream(token.trim(), text, handle);
      ensure();
      void refreshAccount(token.trim());
    } catch (e) {
      ensure();
      patchLast((msg) => ({
        ...msg,
        content: `${msg.content}${msg.content ? "\n" : ""}⚠️ ${(e as Error).message}`,
      }));
    } finally {
      setBusy(false);
    }
  }

  const last = messages[messages.length - 1];
  const waiting = busy && (!last || last.role === "user");

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">◈</span>
          <span className="name">Bailiff</span>
          <span className={`badge ${health}`}>
            {health === "ok" ? "网关在线" : health === "down" ? "网关离线" : "检测中…"}
          </span>
          {model && (
            <span className={`badge ${model === "deepseek" ? "ok" : "warn"}`}>
              {model === "deepseek" ? "deepseek" : "离线演示模型"}
            </span>
          )}
        </div>
        <div className="auth">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            onBlur={() => token && void connect()}
            placeholder="粘贴 JWT token"
          />
          <button className="ghost" onClick={() => void connect()}>
            连接
          </button>
          <button onClick={() => void demoLogin()}>演示登录</button>
        </div>
      </header>

      {error && <div className="banner">{error}</div>}

      <main className="layout">
        <aside className="side">
          <section>
            <h2>工具 · /v1/tools</h2>
            {tools.length === 0 ? (
              <p className="muted">连接后展示 Agent 可调用的工具</p>
            ) : (
              <ul className="tools">
                {tools.map((t) => (
                  <li key={t}>
                    <code>{t}</code>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section>
            <h2>成本 · /v1/cost</h2>
            {account ? (
              <dl className="cost">
                <dt>累计</dt>
                <dd>¥{account.total_cost.toFixed(6)}</dd>
                <dt>token</dt>
                <dd>
                  {account.total_prompt_tokens}↑ {account.total_completion_tokens}↓
                </dd>
                <dt>缓存命中</dt>
                <dd>{(account.cache_hit_rate * 100).toFixed(1)}%</dd>
              </dl>
            ) : (
              <p className="muted">暂无数据</p>
            )}
          </section>

          <section className="tips">
            <h2>试试</h2>
            <ul>
              <li>在 note.txt 写一句话，再读回来</li>
              <li>检索知识库里关于部署的段落</li>
              <li>把「我偏好简洁回答」记进长期记忆</li>
            </ul>
          </section>
        </aside>

        <section className="chat">
          <div className="messages" ref={scrollRef}>
            {messages.length === 0 && (
              <div className="empty">
                <h1>生产级 Agent 的骨架，而不是一个调 API 的 demo</h1>
                <p>
                  每一次工具调用都会经过网关的鉴权 → 权限 → 限流 → 审计；
                  每一步状态都落 checkpoint；每个 token 都计入成本。
                </p>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className="who">{m.role === "user" ? "你" : "Agent"}</div>

                {m.reasoning && (
                  <details className="reasoning" open={m.content === ""}>
                    <summary>思考过程 · {m.reasoning.length} 字</summary>
                    <pre>{m.reasoning}</pre>
                  </details>
                )}

                {m.tools.length > 0 && (
                  <div className="toolcalls">
                    {m.tools.map((t, j) => (
                      <details key={j} className="toolcall">
                        <summary>
                          <span className="tn">⚙ {t.name}</span>
                          {t.result === undefined && <span className="running">运行中…</span>}
                        </summary>
                        {t.args && <pre className="args">{t.args}</pre>}
                        {t.result !== undefined && <pre className="result">{t.result}</pre>}
                      </details>
                    ))}
                  </div>
                )}

                <pre className="content">{m.content}</pre>
              </div>
            ))}

            {waiting && (
              <div className="msg assistant">
                <div className="who">Agent</div>
                <div className="content thinking">思考中…</div>
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
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              rows={1}
              placeholder="输入消息，Enter 发送，Shift+Enter 换行"
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
