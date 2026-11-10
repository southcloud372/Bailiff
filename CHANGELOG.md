# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `AGENT_AUDIT_LOG`: append audit events as JSONL for log shippers, so audit survives restarts.
- Input cap of 32000 characters on chat messages (422 instead of an opaque context overflow).
- `DEEPSEEK_TIMEOUT_SECONDS` / `DEEPSEEK_MAX_RETRIES`, so a stalled upstream cannot pin workers.
- **Zero-dependency retrieval backend** (`knowledge/local.py`, `knowledge/factory.py`,
  `scripts/ingest.py`): SQLite vector store plus a deterministic hashing embedder, so RAG works
  without pgvector or an embedding service. `AGENT_RAG_BACKEND=postgres` switches back to the
  production path. Includes a `knowledge` readiness probe that reports chunk count, because an
  empty index otherwise looks like "the model ignores the knowledge base".
- **Egress tool with SSRF defence** (`tools/web.py`): `http_fetch` with a domain allowlist
  (deny by default), post-DNS IP validation (blocks 127/8, 10/8, 172.16/12, 192.168/16,
  169.254/16 including cloud metadata), per-hop redirect revalidation, and a byte cap.
  Not registered at all unless `AGENT_HTTP_ALLOWED_HOSTS` is set.
- **Read-only mode** (`AGENT_READ_ONLY`): write tools are removed from the registry instead of
  being registered-then-denied, so the model never sees a capability it cannot use.
- **Per-user session namespacing**: `thread_id` is now optional and composed as
  `principal#thread_id`, closing an IDOR where guessing a thread id leaked another user's history.
- **Tool-result truncation** (8 KB) at the tool boundary, preventing one large read from
  destroying context, cost and latency in a single turn.
- Docs: `docs/api.md` (endpoint reference), `docs/operations.md` (deploy/backup/incident
  playbooks), `docs/security.md` (threat model + OWASP LLM Top 10 mapping), `docs/README.md`,
  ADR-0007 (capability default-deny).
- Offline demo model (`core/mock.py`): the full request path runs without an API key.
  `AGENT_LLM_BACKEND=auto` resolves to the real model when a key exists, to the demo model otherwise.
- `POST /v1/auth/dev-token` — dev-only route that mints a JWT so the console needs no CLI step.
  The route is not registered at all outside `AGENT_ENV=dev`.
- Typed SSE events: `reasoning` / `delta` / `tool_call` / `tool_result`, so streaming UIs can show
  thinking and tool progress instead of a blank screen.
- Console is served by FastAPI itself (`/` + `/assets`), collapsing deployment to one container.
- `Makefile`, `docker-compose.demo.yml`, single-command demo.

### Fixed
- **Dev login issued the wrong identity.** `DevTokenRequest` was declared inside `create_app`,
  and with postponed annotations FastAPI could not resolve it, so the request body was silently
  ignored and every token was minted as `demo`. Multi-tenant demos were effectively impossible.
- **`/v1/cost` exposed the global ledger.** Any authenticated caller could read everyone's spend
  and token usage. Cost is now bucketed per principal; the global view is internal only.
- **Audit log grew without bound in memory.** A plain list held every tool call since start,
  which is a slow OOM. Now a bounded ring buffer (5000) plus an optional JSONL sink.
- **Cost was attributed to an empty bucket** because the callback read the principal from a
  contextvar that is not visible in LangChain's callback execution context. Attribution now
  passes the principal explicitly per request.
- **Reasoning models broke on the second model turn.** `reasoning_content` is now passed back on
  assistant messages carrying `tool_calls`; LangChain's `ChatOpenAI` drops it, which made every
  multi-step tool task fail with HTTP 400. See `router/deepseek.py`.
- **A failed tool call could permanently brick a conversation thread.** Missing `tool` messages
  violate the OpenAI-compatible contract, and the resulting 400 never mentioned the real cause.
  Two-layer fix: all tool exceptions become structured results, and `_repair_dangling_tool_calls`
  backfills missing tool messages so already-corrupted threads recover.
- Unknown API paths returned 405 instead of 404 when the static console was mounted at `/`.

### Changed
- Default retrieval backend is now local instead of pgvector. Previously a missing Ollama or
  Postgres made `search_knowledge` fail with 502, which read as "RAG is broken" rather than
  "infrastructure is missing".
- Removed explanatory scaffolding from code docstrings and docs: markdown headings inside Python
  docstrings, interview-oriented phrasing, and the overuse of em dashes.
- `/v1/call` 403 now names both possible causes (missing role vs. capability not enabled on this
  instance), instead of echoing only the tool name.
- `prod` treats the `llm` readiness probe as critical: shipping the offline model now fails
  `/readyz` rather than silently answering every request with template text.
- Permission denials no longer propagate as exceptions from the tool boundary; they are returned as
  `{"ok": false, "kind": "permission_denied"}`. Governance and audit are unchanged — this is a
  protocol-integrity fix, documented in `core/agent.py`.

## [0.2.0] - 2026-11-10

### Added
- Request-context middleware (request-id propagation, structured access logs, HTTP metrics).
- SQLite and Postgres checkpoint backends; `interrupt()`-based human approval survives restarts.
- `/readyz` readiness probe with concurrent dependency checks, `/metrics` in Prometheus text format.
- Redis sliding-window rate limiting with fail-degraded fallback to the local token bucket.
- Semantic cache, enabled only for the stateless `/v1/ask` endpoint.
- pgvector connection pooling, HNSW and GIN indexes, explicit embedding dimensions.

### Changed
- Dependency baseline: LangGraph 1.2.20, LangChain 1.4.9, MCP SDK 2.3.0, FastAPI 0.162.2,
  Pydantic 2.14.3, OpenTelemetry 1.45.2, Redis 8, PostgreSQL 18.
- Default model is `deepseek-flash`; `deepseek-chat` was retired upstream on 2026-07-24.
- Offline price table for LiteLLM, removing a ~15s network stall at import time.

## [0.1.0] - 2026-09

Initial internal version: gateway governance, LangGraph orchestration, MCP client, RAG, evaluation harness.
