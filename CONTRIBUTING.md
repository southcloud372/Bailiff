# Contributing

Thanks for taking the time to contribute. This document is deliberately short and specific.

## Setup

```bash
git clone https://github.com/southcloud372/Bailiff && cd Bailiff
uv sync --extra prod          # Python 3.13, uv
cd web && npm install && cd ..
cp .env.example .env          # optional; leave DEEPSEEK_API_KEY empty to run offline
```

You do **not** need an API key to develop. Without one, the model layer falls back to a
deterministic offline model, so tests, the console and the whole request path work.

## Before you open a PR

```bash
make check     # ruff + mypy --strict + pytest
```

All three must pass. CI runs exactly this, on Linux, with `uv sync --frozen`.

## What we look for in a PR

- **One concern per PR.** A bug fix should not also reformat a module.
- **A test that fails before your change.** For behavioural fixes this is the whole review.
- **Comments that explain *why*, not *what*.** The code already says what it does. If a
  decision looks odd, say what would break if you did the obvious thing instead.
- **No new runtime dependency unless the standard library genuinely cannot do it.**

## Design principles (please don't fight these)

1. **Orchestration decides, governance enforces.** Tools are never handed to the model
   directly; they are resolved through the gateway so auth/permission/limit/audit cannot
   be bypassed by a clever prompt.
2. **A failing tool must not kill the turn.** Errors become observations. The protocol
   requirement behind this is documented in `core/agent.py` and `router/deepseek.py`.
3. **Liveness ≠ readiness.** `/healthz` never touches a dependency. `/readyz` does, and
   returns 503 so the load balancer drains traffic instead of restarting the process.
4. **Defaults must run on a laptop.** Anything that needs Postgres/Redis/Ollama is opt-in
   via env var. `clone → run` must work with zero infrastructure.
5. **Degrade visibly, never silently.** Falling back to the offline model, a local rate
   limiter, or a keyword-only retriever is fine — as long as `/readyz` or the logs say so.

## Good first issues

Look for the [`good first issue`](../../labels/good%20first%20issue) label. Things that are
genuinely useful and self-contained:

- Add a tool that demonstrates the gateway (e.g. `http_fetch` with an allow-list).
- Add an eval case to `evaluation/dataset.py` for a failure you actually observed.
- Improve an error message that cost you time while debugging.

## Commit messages

Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`).
Scope is welcome: `fix(router): pass reasoning_content back on tool-call turns`.

## Documentation

- User-facing behaviour changes → update `README.md` and `README.zh-CN.md`.
- Architectural decisions → add an ADR under `docs/adr/` (copy the format of an existing one).
