# Plan: Move Benchmark Runner to Fully Synchronous Execution

## Rationale
- Current async scaffolding adds event-loop, executor, and cross-thread logging without providing concurrency; each story runs sequentially.
- The loop never schedules two tasks at once; every blocking call runs to completion before the next begins. The executor hop may slightly slow things down.
- `Conversation.run` is synchronous; wrapping it in a thread only adds overhead and complexity.
- Simplifying to sync code reduces surface area for thread-safety issues (logging, shared git worktree, S3 sync) and makes errors easier to reason about.
- Parallelism would require per-story worktrees and git isolation; those changes are out of scope, so simplicity wins.

## Target State
- `runner.py` uses synchronous functions end-to-end; no `asyncio.run`, `run_in_executor`, or `run_coroutine_threadsafe`.
- Event logging happens inline in the calling thread; `state.log_event` remains synchronous.
- Heavy I/O (git, tar/S3) stays synchronous; no event loop to block.

## Migration Steps
1) Refactor API surface
   - Convert `implement_story`, `evaluate_acceptance_criteria`, `run_benchmark`, `_maybe_implement`, `_maybe_evaluate`, and `log_message` to synchronous functions.
   - Replace `asyncio.run(runner.execute())` with direct `runner.execute()`; update callers/tests accordingly.

2) Remove async plumbing
   - Delete event-loop acquisition (`get_event_loop`/`get_running_loop`), `run_in_executor`, `run_coroutine_threadsafe`, and `asyncio.to_thread` usage in logging.
   - Inline callback logging; ensure `event_callback` writes via `self.state.log_event` directly.

3) Simplify function signatures and call sites
   - Drop `await`/`async` at all call sites; adjust helper return types to sync.
   - Update any type hints that referenced `Awaitable`/`Coroutine`.

4) Clean up dependencies and imports
   - Remove unused `asyncio` imports (keep only if needed elsewhere); trim executor/thread-related code paths.

5) Re-run style checks
   - `uv run --frozen ruff format .`
   - `uv run --frozen ruff check .`

## Validation Plan
- Run an end-to-end benchmark in a non-production environment; confirm story generation, implementation, evaluation, and manifest updates still work.
- Verify logs and `state.log_event` entries are written correctly without race conditions.
- Confirm S3 archive uploads continue to function after refactor.

## Risks and Mitigations
- **Behavior drift**: Ensure callback handling still prints human-readable events; add a small manual test story to check output.
- **Hidden async assumptions**: Search for any external caller relying on async `execute`; if found, provide a thin sync wrapper or maintain an adapter for backward compatibility.
