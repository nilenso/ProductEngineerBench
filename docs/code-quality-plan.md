# ProductEngineerBench Code Quality Plan

Scope covers `src/productengineerbench/runner.py`, `src/productengineerbench/state.py`, and the durability spec in `docs/durable-runner.md`. Goals: simplify control flow, reduce unnecessary defensive code, and make state handling more testable and composable.

## Key Findings
- **Tangled responsibilities**: `BenchmarkRunner` mixes AWS sync, git plumbing, story lifecycle, and log serialization in one class, making testing and reuse hard.
- **Over-defensive happy-path code**: frequent `try/except Exception` blocks (e.g., AWS checkpointing, manifest init, run entrypoint) hide errors and complicate debugging; many branches silently return instead of surfacing problems.
- **Stateful imperative flow**: `run_benchmark` mutates shared runner state (`current_story_file`, env vars) and re-reads status mid-loop; phases are coupled to side effects rather than pure decisions, reducing testability.
- **Loose type/contract enforcement**: environment and config validation is scattered; missing/invalid values often default instead of failing early (e.g., model tokens via `_env_int`, AWS env normalization, resume compatibility only in some paths).
- **I/O helpers inside runner**: logging, serialization, and git helpers are embedded in the runner instead of isolated utilities, creating tight coupling and duplicate concerns.
- **Doc/code drift risk**: durable-runner spec is rich, but code paths do not clearly map to the described checkpoints/resume behaviors, and there are no fast tests covering them.

## Improvement Plan
1) **Isolate infrastructure concerns (lean, protocol-driven)**
   - Define small `Protocol`s for sync/tar and git interactions (e.g., `SyncClient`, `GitClient`) so tests can swap fakes. Keep concrete classes but prefer `@staticmethod` helpers for pure functions (path computations, command building) to trim instance state.
   - Extract S3 sync/resume into a client using `s3fs` plus stdlib `tarfile` for archiving to avoid bespoke tarball logic.
   - Move git helpers into a thin utility (classmethods/staticmethods where possible) so `BenchmarkRunner` orchestrates rather than operates.

2) **Enforce explicit error handling**
   - Replace bare/blanket `except Exception` with targeted exceptions and structured error reporting; unexpected errors should propagate to fail fast.
   - Fail early on missing env/config (API keys, repo URLs, resume mismatches) instead of silently returning; surface actionable messages.

3) **Simplify story lifecycle orchestration**
   - Refactor `run_benchmark` into pure decision helpers (determine next phase, should-skip logic) plus small effectful executors; pass context explicitly to eliminate `current_story_file` mutation.
   - Represent phases with an enum to avoid stringly-typed checks and reduce repeated status reloads.

4) **Functional logging pipeline**
   - Introduce a structured logger facade adhering to a `LoggerProtocol`; serialization happens once and delegates to `RunState` appenders. Use `structlog` for consistent event formatting.
   - Ensure event callbacks are side-effect free; return explicit success/failure signals instead of mutating outer flags.

5) **Configuration handling (Pydantic Settings)**
   - Wrap existing config loading in `BaseSettings`/`SettingsConfigDict` classes to validate and document inputs while preserving current file-reading order and precedence exactly as today (no changed overrides yet). Provide adapters that emit the same dict structure consumed by the runner to avoid downstream changes.
   - Add explicit validation for required fields (API keys, repo URLs, model names, revisions) and normalize env parsing (ints, booleans) via Pydantic validators.

6) **Testability & coverage**
   - Add unit tests for `RunState` (atomic writes, resume SHA selection, status transitions) and orchestration decisions (skip/retry paths) using temp dirs and protocol fakes.
   - Add integration-style smoke test that simulates resume scenarios without external services by plugging in fake `GitClient`/`SyncClient` and mock LLMs.

7) **Spec alignment and documentation**
   - Cross-check code paths against `docs/durable-runner.md` (checkpoints, atomic writes, resume compatibility) and document intentional deviations.
   - Keep docs updated by co-locating behavior notes near the orchestrator and adding a short “durability invariants” comment block.

## Near-Term Execution Order
1. Carve out protocol-driven `SyncClient` (S3 impl + tar helper) and `GitClient`; keep helpers as `@staticmethod` where pure.
2. Reshape `run_benchmark` into decision + execution helpers; remove mutable shared fields and prefer return values.
3. Introduce Pydantic Settings for config validation while preserving current YAML/env read behavior.
4. Tighten error handling and input validation, especially around env/config and resume paths.
5. Add structured logging facade (`LoggerProtocol`, possible `structlog` adoption) and streamline event serialization.
6. Add `RunState` and orchestration tests; wire into CI via `uv run --frozen ruff check .` and `uv run --frozen pytest` when present.
7. Reconcile code with durability spec, updating comments/docs where behavior differs.
