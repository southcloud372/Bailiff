# Security Policy

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately via GitHub Security Advisories ("Security" tab → "Report a vulnerability").
We aim to acknowledge within 72 hours and to ship a fix or mitigation within 14 days.

Include: affected version/commit, reproduction steps, and impact assessment if you have one.

## Threat model

This project runs LLM-driven tool calls, so the interesting attacks are not classic web CVEs.
The boundaries we actually defend:

| Boundary | Control | Location |
|---|---|---|
| Prompt → tool execution | Model output never reaches a function directly; every call crosses the gateway | `gateway/gateway.py` |
| Caller identity → tool permission | Role→tool policy, evaluated server-side per call | `gateway/policy.py` |
| Filesystem access | Sandbox root confinement + path traversal rejection | `gateway/security.py` |
| Prompt injection | Input guardrails before the model (best effort, **not** a security boundary) | `guardrails/guards.py` |
| Secret exposure | Keys only from env, never logged; `/readyz` refuses demo secrets in prod | `gateway/config.py`, `observability/health.py` |
| Multi-replica quota bypass | Redis sliding window; degrades to local bucket on failure (fail-degraded, not fail-open) | `gateway/ratelimit.py` |

## Known limitations

We would rather state these than have you find them in production:

- **Sandboxing is path-level, not execution-level.** Tools run in-process. Untrusted code execution requires a real sandbox (gVisor / Firecracker / separate worker).
- **Prompt-injection detection is heuristic.** It reduces noise, it does not stop a determined attacker. The gateway is the actual boundary.
- **Streaming responses are not masked token-by-token.** Once a chunk is sent it cannot be recalled; masking applies to non-streaming responses and audit records.
- **Semantic cache threshold (0.95) is a default, not a calibration.** Tune it on your own traffic before enabling in production, and never enable it for stateful multi-turn endpoints.
