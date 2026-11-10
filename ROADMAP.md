# Roadmap

Ordered by "what blocks a real deployment", not by what is fun to build.
Items marked **help wanted** are self-contained and have a clear definition of done.

## Now

- [ ] **Trajectory-level evaluation.** Today we score the final answer. We need to score the path —
      which tools were chosen, in what order, with what arguments — because a correct answer reached
      by an unsafe path is still a production incident. **help wanted**
- [ ] **Prompt-injection corpus.** A versioned set of adversarial inputs (indirect injection via
      retrieved documents, tool-output injection) with expected gateway outcomes, run in CI.
- [ ] **K8s manifests.** Deployment + HPA + PodDisruptionBudget, with `/readyz` wired as readiness
      and `/healthz` as liveness to demonstrate the distinction in a real scheduler config.

## Next

- [ ] **A2A interop.** Agent-to-agent protocol surface; the design note exists, the transport does not.
- [ ] **Real sandboxing.** Path-level confinement is not execution-level isolation. Evaluate
      gVisor / Firecracker for a worker-based tool executor.
- [ ] **Semantic cache calibration tooling.** A script that replays real traffic and reports the
      precision/recall trade-off across thresholds, instead of shipping a guessed 0.95.
- [ ] **Postgres-backed semantic cache.** Currently in-process; multi-replica deployments get no
      shared cache hits.

## Later

- [ ] OpenTelemetry GenAI semantic conventions: track the spec as `gen_ai.*` moves from Development
      to Stable and pin the version we emit.
- [ ] Multi-tenant isolation review: per-tenant keys, quota and vector-store partitioning.
- [ ] Cost-aware model routing: pick the cheap model when the eval suite says it is sufficient for
      the task class, escalate on failure.

## Explicit non-goals

- **A visual flow builder.** Dify and Flowise do it better; competing there would dilute the
  governance story.
- **Model training / fine-tuning.** This is a runtime, not a model project.
- **Being a general-purpose web framework.** FastAPI is the transport; the product is the runtime.
