# UsefulCodeBench

UsefulCodeBench is a benchmark harness that executes StoryMachine-driven coding tasks end-to-end. It clones target repositories, generates implementation stories, applies Claude-based automation to implement/evaluate each story, and stores the resulting artifacts for later analysis.

## Repository layout

- `Dockerfile` – container image used to run the benchmark.
- `config/storymachine.yaml` – StoryMachine configuration shared across runs.
- `data/` – per-repository benchmark definitions and auxiliary assets.
- `scripts/run_benchmark.sh` – convenience wrapper around the Docker invocation.
- `src/usefulcodebench/` – Python package with the benchmark runner implementation.
- `uv.lock`, `pyproject.toml` – Python dependency management.

## Prerequisites

1. **Docker**: The benchmark executes inside a container. Install Docker Engine 24.x or newer.
2. **Anthropic Claude credentials**:
   - `~/.claude.json`
   - `~/.claude/`
   These are mounted read-only into the container for both implementer and evaluator agents.
3. **GitHub credentials**: Set `GIT_TOKEN` in your environment or `.env` file. The token must have `repo` scope for private GitHub repositories.
4. **Optional `.env` file**: Create a `.env` at the repository root to provide additional environment variables consumed by the benchmark runner (for example Anthropic API keys, model overrides, etc.). The file is passed directly to `docker run --env-file`.

## Build the benchmark runner image

Run the Docker build from the repository root:

```/dev/null/README_build.sh#L1-1
docker build -t benchmark-runner .
```

This image contains all Python dependencies, Claude CLI, Playwright, and utility binaries required by the benchmark.

## Running benchmarks via helper script

Use the provided wrapper to execute every repository configuration in `data/*.yaml`:

```/dev/null/README_run.sh#L1-1
./scripts/run_benchmark.sh
```

The script performs the following for each YAML file in `data/`:

1. Creates a timestamped results directory under `results/` (or `$RESULTS_DIR`).
2. Launches `docker run` with the appropriate mounts and environment variables.
3. Forwards credentials (`GIT_TOKEN`, Claude state) and optional variables from `.env`.

### Environment overrides

The script honors the following variables for advanced usage:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATA_DIR` | `<repo>/data` | Location of repository YAML definitions |
| `CONFIG_DIR` | `<repo>/config` | Location of shared benchmark configs |
| `RESULTS_DIR` | `<repo>/results` | Host directory that receives results |
| `BENCHMARK_IMAGE` | `benchmark-runner` | Docker image name to execute |
| `CLAUDE_JSON_PATH` | `$HOME/.claude.json` | Path to Claude credentials JSON |
| `CLAUDE_DIR_PATH` | `$HOME/.claude` | Directory with Claude session state |
| `BENCHMARK_ENV_FILE` | `<repo>/.env` | Extra environment variables to pass via `--env-file` |

To run a single repository configuration, point `DATA_DIR` at a directory containing just the target YAML file.

### Required host secrets

The script checks for Claude credential files. It does **not** verify `GIT_TOKEN`—set it explicitly before running:

```/dev/null/README_env.sh#L1-3
export GIT_TOKEN=ghp_your_token_here
./scripts/run_benchmark.sh
```

Avoid hardcoding secrets inside version-controlled files.

## Understanding repository configurations

Each file in `data/*.yaml` declares how to evaluate a target repository. For example, `data/grand_central.yaml` contains:

```data/grand_central.yaml#L1-18
repository:
  name: "grand-central"
  url: "https://github.com/nilenso/grand-central"
  revision: "c0b34b1f25c4f9d503262d8256e2c2f9caaab275"
  prd: "/data/grand_central/prd.md"
  tech_spec: "/data/grand_central/tech-spec.md"

setup:
  commands:
    - "echo $PATH"
    - "curl https://mise.run | sh"
    - eval "$(~/.local/bin/mise activate bash --shims)"
    - "~/.local/bin/mise use node@18"
    - "~/.local/bin/mise doctor"
    - "node -v"
    - "npm install"
    - "npm run setup"

  env:
    DATABASE_URL: "file:./sqlite.db"
```

Key sections:

- `repository`: Clone URL, pinned revision, and paths to PRD / technical specification files bundled in `data/<repo_name>/`.
- `setup`: Optional shell commands & environment variables run inside the cloned repo before StoryMachine generates stories.

## Runner behavior summary

1. Clone the configured repository and checkout the specified revision.
2. Execute any setup commands (capturing exported environment variables).
3. Generate stories via StoryMachine using `config/storymachine.yaml`.
4. Delegate implementation & evaluation to Claude agents.
5. Save results, logs, and intermediate artifacts to `/results` (mounted from host).

Useful artifacts produced per story:

- `<story>.implement.jsonl` / `<story>.evaluate.jsonl`: Streaming Claude transcripts.
- `<story>.result.md`: Final evaluation report captured from `result.md` in the repo.
- Updated repository state committed to a local branch inside the container for traceability.

## Local development

If you prefer to run the Python entrypoint directly (outside Docker), install dependencies with `uv`:

```/dev/null/README_dev.sh#L1-3
uv sync
uv run usefulcodebench
```

Ensure you have the same system dependencies installed as the Docker image (git, curl, sqlite3, Playwright prerequisites, etc.).

## Troubleshooting

- **Missing Claude credentials**: The helper script will abort if `~/.claude.json` or `~/.claude/` are absent.
- **Git credential issues**: Confirm `GIT_TOKEN` is exported before running; private repositories require this token.
- **StoryMachine failures**: Validate `config/storymachine.yaml` and ensure the packaged version referenced in the config exists.
- **Setup command failures**: The runner halts on the first non-zero exit. Re-run the benchmark after addressing the underlying issue inside the target repository.

## Cleaning up

Results accumulate under `results/`. Remove older runs as needed:

```/dev/null/README_clean.sh#L1-1
rm -rf results/*
```

Docker containers run with `--rm`, so no stopped containers are left behind after each benchmark execution.
