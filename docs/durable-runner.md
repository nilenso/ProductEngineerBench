# Durable Benchmark Runner — Technical Specification

## Overview

This document specifies durability for the ProductEngineerBench runner so that runs persist state as they progress and can be resumed after crashes or interruptions without redoing completed work.

Primary goals:

- Persist all critical artifacts (stories, repo changes, logs, results) under a single run directory.
- Record structured, incremental status per story and per phase (implement, evaluate).
- Support resume-at-phase granularity on re-run (skip completed work, continue partial work safely).
- Avoid persisting secrets; capture non-sensitive configuration for reproducibility.

Non-goals (initial scope):

- Mid-trajectory resume inside an LLM conversation; phase re-execution is acceptable.
- Distributed/concurrent execution. Design is single-process; future work can add locking.

## Terminology

- Run: A single benchmark execution against one repository config (a unique directory under `results/`).
- Story: A single generated story to implement and evaluate.
- Phase: `implement` or `evaluate` for each story.
- Status: Progress marker persisted per story (`pending`, `implementing`, `evaluating`, `completed`, `failed`).

## Per-Run Directory Layout

All files live under the run root: `${RESULTS_DIR}/<repo_name>_<timestamp>` (or a caller-provided `RUN_DIR`).

```
<run_root>/
  manifest.json                 # run-level metadata, versions, models (no secrets)
  configs/
    repo.yaml                   # copy of the repository config used
    storymachine.yaml           # copy of the storymachine config used
  repo/                         # durable working copy of the target repository (git dir)
  stories/                      # generated stories (.md)
  stories_index.json            # ordered list of story filenames
  stories/<story_name>/
    status.json                 # authoritative per-story status & timestamps
    implement.jsonl             # append-only Openhands events (implement phase)
    evaluate.jsonl              # append-only Openhands events (evaluate phase)
    result.md                   # final evaluation report or error summary
    artifacts/                  # optional additional outputs
```

Notes:

- `repo/` and `stories/` move from ephemeral locations to inside `<run_root>` so they persist across container restarts.
- Event logs remain JSONL and append-only; status is the source of truth for phase completion.

## Data Model & Schemas

Schema versioning: `schema_version: 1` embedded in `manifest.json` and `status.json` allows forward changes.

### `manifest.json` (run-level)

```json
{
  "schema_version": 1,
  "run_id": "2025-11-03T10-15-45Z_grand-central",
  "created_at": "2025-11-03T10:15:45Z",
  "image": "benchmark-runner:latest",
  "tools": {
    "openhands_sdk": "<version>",
    "storymachine": {"package": "<pkg>", "version": "<tag>"}
  },
  "models": {
    "implementer": {
      "name": "claude-haiku-4-5",
      "base_url": "https://...", 
      "max_input_tokens": 100000,
      "max_output_tokens": 20000
    },
    "evaluator": {
      "name": "claude-sonnet-4-5",
      "base_url": "https://..."
    }
  },
  "repo": {
    "name": "grand-central",
    "url": "https://github.com/nilenso/grand-central",
    "base_revision": "c0b34b1...",
    "branch": "eval_grand-central"
  }
}
```

Secrets such as API keys are not persisted.

### `stories_index.json`

```json
{
  "order": ["story-001.md", "story-002.md", "story-003.md"]
}
```

### Per-Story `status.json`

```json
{
  "schema_version": 1,
  "story_file": "story-001.md",
  "phase": "evaluating",            
  "started_at": "2025-11-03T10:22:10Z",
  "updated_at": "2025-11-03T10:31:55Z",
  "completed_at": null,
  "commits": {
    "before": "a1b2c3...",          
    "after_implement": "d4e5f6..."   
  },
  "error": null                      
}
```

`phase` values: `pending`, `implementing`, `evaluating`, `completed`, `failed`.

### Event logs (`implement.jsonl`, `evaluate.jsonl`)

Each line is a JSON object. Minimum fields:

```json
{
  "session_id": "<uuid>",
  "seq": 42,
  "timestamp": 1730620445.123,   
  "type": "OpenhandsEvent",
  "content": { ... }              
}
```

`seq` increments monotonically within a file; `session_id` changes per-run to disambiguate appended sessions after resume.

## Lifecycle & Algorithms

### Initialization

- Resolve `RESULTS_DIR` (default `/results` in container) and set `<run_root>`:
  - If `RUN_DIR` is provided and exists, use it (resume scenario).
  - Else create a new `<repo_name>_<timestamp>` directory.
- Create `<run_root>/configs/` and copy `REPO_CONFIG` and `STORYMACHINE_CONFIG` into it.
- Write `manifest.json` (first version) with tool and model metadata.

### Repository Setup

- Desired location: `<run_root>/repo`.
- If `<run_root>/repo/.git` exists and `RESUME=1` is set: reuse; otherwise clone and checkout `repository.revision` to branch `eval_<name>`.
- Run repo `setup.commands` (if any) and capture exported env from the script (existing behavior retained).
- Record `base_revision` and `branch` in `manifest.json`.

### Story Generation

- Desired location: `<run_root>/stories`.
- If any `*.md` exists in `<run_root>/stories` and `RESUME=1`: skip generation.
- Else run StoryMachine and write files to `<run_root>/stories`.
- Write `stories_index.json` with the ordered list of `*.md`.

### Per-Story Execution

For each story in `stories_index.json` order:

1) Load `stories/<story>/status.json` if present; else create with `phase=pending`.

2) Implement phase:
   - Skip if `phase` is `evaluating` or `completed`.
   - Write status atomically: `phase=implementing`, `started_at` if unset.
   - Create/append `implement.jsonl` (new `session_id`).
   - Run implement agent. On success:
     - Commit repo changes; capture `before` and `after_implement` SHAs.
     - Update status: `phase=evaluating`, set `updated_at`.
   - On failure:
     - Update status: `phase=failed`, set `error` summary, keep logs.

3) Evaluate phase:
   - Skip if `phase` is `completed`.
   - Write status atomically: ensure `phase=evaluating`.
   - Create/append `evaluate.jsonl` (new `session_id`).
   - Run evaluator. On success:
     - Write `result.md` (from `<run_root>/repo/result.md` if produced, else synthesize a minimal report).
     - Update status: `phase=completed`, set `completed_at`.
   - On failure:
     - Update status: `phase=failed`, set `error` summary.

### Atomic Writes

- For `status.json` and `stories_index.json`:
  - Write to `*.tmp`, `fsync` file, `rename` to final name, then `fsync` parent dir.
  - `rename` is atomic on the same filesystem (Docker bind mounts satisfy this).

## Resume Logic

Decision per story on startup:

- `completed`: skip.
- `evaluating` with no `completed_at`: run evaluate only.
- `implementing` (no post-commit): rerun implement; commit may differ; update SHAs.
- Absent `status.json`: treat as `pending`.

Repo reuse:

- If `<run_root>/repo/.git` exists: reuse; `git checkout eval_<name>` and `git reset --hard` to the last recorded SHA if present in `status.json`. If absent, remain on base revision branch.

Stories reuse:

- If `<run_root>/stories/*.md` exist: skip generation, trust `stories_index.json`. If `stories_index.json` missing, rebuild it from directory listing.

Compatibility checks on resume:

- Compare `manifest.repo.url` and `manifest.repo.base_revision` to the provided `REPO_CONFIG`. Mismatch defaults to hard error unless `FORCE_RESUME=1`.
- Compare `storymachine` package:version to `STORYMACHINE_CONFIG`; warn or block if changed.
- Compare `models.*.name`; warn (allowed to differ).

## Crash Scenarios & Recovery

- Crash during clone/setup:
  - No `repo/.git` or incomplete setup. On resume, clone/setup runs again. No status files written yet.

- Crash during story generation:
  - Partial `stories/`. On resume, if `stories/*.md` exist but `stories_index.json` missing, write index from discovered files. Generation is skipped if `RESUME=1`.

- Crash during implement (before commit):
  - `status.phase=implementing`; logs in `implement.jsonl` with an unfinished session. On resume, rerun implement; append with new `session_id`.

- Crash after commit, before evaluating:
  - `status.phase=evaluating` with `commits.after_implement` set. On resume, run evaluate only.

- Crash during evaluate:
  - `status.phase=evaluating`; partial `evaluate.jsonl`. On resume, rerun evaluate; append with new `session_id`.

- Crash after writing `result.md` but before marking completed:
  - `status.phase=evaluating`; `result.md` present. On resume, mark `phase=completed` after a quick validation or simply rerun evaluate to be safe (configurable via `EVALUATE_IF_RESULT_PRESENT=0/1`). Default: rerun.

- JSONL/Status partial write:
  - JSONL is append-only; partial last line is ignored by robust readers (we write with newline + flush). Status uses atomic `rename`, so file is either previous or next version.

- Disk full:
  - Writes fail; process errors. On resume, runner continues once space is available; status remains last consistent version.

- Provider/network errors:
  - Caught as `CodeImplementationError`/`UATEvaluationError`; `status=failed`, `error` populated, `result.md` records failure. Resume can retry the failed phase manually by clearing `failed` to `implementing|evaluating` or by `FORCE_RETRY_FAILED=1` env.

## Interfaces (Runner & Script)

### Environment variables

- `RESULTS_DIR` (path): Root for run directories (default `/results` in container).
- `RUN_DIR` (path, optional): Explicit run directory; if exists, resume; else create.
- `RESUME` (`0|1`): When `1`, skip cloning/generation if durable state exists.
- `FORCE_RESUME` (`0|1`): Ignore compatibility mismatches.
- `SKIP_GENERATE_STORIES` (`0|1`): Skip story generation unconditionally.
- `SKIP_IMPLEMENT` (`0|1`), `SKIP_EVALUATE` (`0|1`): Developer toggles for targeted reruns.
- `EVALUATE_IF_RESULT_PRESENT` (`0|1`): If `1`, skip re-evaluation when `result.md` exists.

Existing model/env variables continue to be honored:

- `IMPLEMENTER_MODEL`, `EVALUATOR_MODEL`, `IMPLEMENTER_LLM_BASE_URL`, `EVALUATOR_LLM_BASE_URL`, `MAX_INPUT_TOKENS`, `MAX_OUTPUT_TOKENS`, etc.

### Script: `scripts/run_benchmark.sh`

Add support for resuming or specifying a fixed run directory:

- New optional envs understood by the script:
  - `RUN_DIR`: If set, use this as `<run_root>`; do not create a timestamped folder.
  - `RESUME=1`: Forwarded into the container; runner will reuse `<run_root>/repo` and `<run_root>/stories` if present.

Mounts (unchanged for default, same for resume):

- `-v "${run_dir}:/results"` remains the single mount. The runner now reads/writes `repo/` and `stories/` under `/results` so no extra mounts are required.

Example invocations:

```
# Fresh run (script picks a timestamped directory)
./scripts/run_benchmark.sh

# Resume an interrupted run
RUN_DIR=results/grand_central_20251103_101545 RESUME=1 ./scripts/run_benchmark.sh
```

### Dockerfile

No changes required. The runner writes to `/results` which is already mounted; repo and stories now live there.

## Implementation Plan (Code Changes)

### 1) Persist repo/stories under results

- In `src/productengineerbench/runner.py` set:
  - `self.results_dir` from `RESULTS_DIR`.
  - `self.repo_dir = self.results_dir / "repo"`.
  - `self.stories_dir = self.results_dir / "stories"`.

### 2) RunState helper

- New module `src/productengineerbench/state.py` exporting `RunState` with responsibilities:
  - Read/write `manifest.json` and `stories_index.json` using atomic writes.
  - Manage per-story `status.json` transitions and timestamps.
  - Allocate `session_id` and `seq` counters for logging.
  - Provide helpers: `record_commit_shas(before, after)`, `mark_failed(err)`, `mark_completed()`.

### 3) Repository setup

- If `RESUME=1` and `repo/.git` exists: skip clone, ensure branch checkout and optional `reset --hard` to last known SHA.
- Else clone to `<run_root>/repo`, checkout `eval_<name>` from `repository.revision` and run setup.

### 4) Story generation

- If `RESUME=1` and `stories/*.md` exist: skip; else generate into `<run_root>/stories`.
- Always maintain `stories_index.json`.

### 5) Implement/Evaluate wiring

- Before implement: `status.phase=implementing`.
- After implement: commit and persist SHAs; set `phase=evaluating`.
- After evaluate: write `result.md` and set `phase=completed`.
- On exceptions: set `phase=failed` with `error` details.

### 6) Logging additions

- Extend `log_message()` to include `session_id` and incremental `seq` per `phase` file.

## Testing Plan

- End-to-end fresh run → verify expected layout and status transitions for all stories.
- Kill during implement → resume; confirm implement reruns once, then evaluate.
- Kill after commit before evaluate → resume; only evaluate runs.
- Kill during evaluate → resume; evaluate reruns and completes.
- Delete `stories_index.json` → resume; index rebuilt from files.
- Change `REPO_CONFIG` URL or revision → resume should halt unless `FORCE_RESUME=1`.
- Ensure no secrets appear in `manifest.json` or per-story `status.json`.

## Edge Cases & Notes

- Idempotence: Implement reruns may produce different diffs; commit SHAs capture divergence.
- Partial last JSONL line: writer ensures newline; readers should ignore unterminated lines.
- Filesystem atomicity: `rename` within `/results` bind mount is atomic; avoid cross-device moves.
- Time sources: Use UTC ISO-8601 for human timestamps; monotonic for event `timestamp` fields.
- Concurrency: Single worker assumed. If parallelizing later, add file locks per story directory.
- Back-compat: If `manifest.schema_version` mismatches, fail with a clear message and suggest export/migration.

## Future Work

- Fine-grained resume within an agent trajectory (Openhands step-level control).
- Checkpointing agent/tool state beyond JSONL transcripts.
- Optional SQLite index over JSONL for faster analytics.
- Content-addressable cache of evaluations by repo SHA + story hash.
