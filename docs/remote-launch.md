# Remote Benchmark Workflow

This document explains how to run ProductEngineerBench on the long-lived DigitalOcean droplet described in `nilenso/infra`, how durability works, and how to retrieve artifacts. The infra repository is still responsible for provisioning the droplet, registry, IAM user, and secrets; this repository provides the tooling that runs inside that environment.

## Architecture recap

- **Droplet**: Ubuntu 24.04+ host with Docker, UV, AWS CLI, and user `bench` (passwordless sudo for Docker). The helper scripts automatically clone `nilenso/ProductEngineerBench` into `~/ProductEngineerBench` (override with `--remote-repo`) and keep it synced before every run, so no pre-baked copy in `/opt` is required.
- **Secrets**: `/home/bench/.env.remote` contains LLM + git credentials, `/home/bench/.aws/credentials` contains a scoped IAM user, and `/etc/profile.d/bench-remote.sh` exports `BENCHMARK_IMAGE`, `REMOTE_RESULTS_ROOT`, `S3_BUCKET`, etc.
- **Runner**: The Python `BenchmarkRunner` now understands `SYNC_BUCKET`, `SYNC_PREFIX`, and `RESUME_PREFIX`. It downloads run directories from S3 when resuming and uploads every run directory back to S3 (with `--delete`) as it exits, updating `manifest.status` with `success`, `failure`, or `cancelled`.
- **Tooling**: New scripts wrap the Docker context workflow, remote runs, cancellations, and syncing artifacts back to a laptop.

## Prerequisites

1. Docker CLI on your laptop (Docker Engine v24+).
2. SSH access to the droplet as `bench`.
3. Credentials for the DigitalOcean registry and the AWS bucket (`peb-results-prod` or whichever bucket Terraform configured).
4. `uv` available locally if you plan to build/push the container image from this repo.

## 1. Create or update the Docker context

Infra exposes Docker via SSH (`ssh://bench@<REMOTE_HOST>`). Create a context once and reuse it:

```bash
BENCH_REMOTE_HOST=bench-runner.example.com \
scripts/setup_docker_context.sh --apply
```

Flags:

| Flag/env | Meaning |
| --- | --- |
| `--context / $BENCH_REMOTE_CONTEXT` | Name for the Docker context (default `do-bench`). |
| `--host / $BENCH_REMOTE_HOST` | Droplet hostname or IP. |
| `--user / $BENCH_REMOTE_USER` | SSH user (default `bench`). |
| `--force` | Remove any existing context of the same name. |

You can always inspect the context via `docker --context do-bench ps`.

## 2. Build and push the runner image

From the repo root:

```bash
docker build -t registry.digitalocean.com/<registry>/benchmark-runner:<tag> .
docker push registry.digitalocean.com/<registry>/benchmark-runner:<tag>
```

Export the resulting tag as `BENCHMARK_IMAGE` (either in your shell or on the droplet via `/etc/profile.d/bench-remote.sh`). The helper script reads that variable automatically.

## 3. Launch a remote benchmark

Use `scripts/run_remote_benchmark.sh` from your local checkout:

```bash
BENCH_REMOTE_HOST=bench-runner.example.com \
S3_BUCKET=peb-results-prod \
scripts/run_remote_benchmark.sh \
  --context do-bench \
  --repo grand_central \
  --image registry.digitalocean.com/<registry>/benchmark-runner:<tag> \
  --bucket peb-results-prod \
  --s3-prefix remote/grand_central/2025-11-14T18-00Z
```

What the script does:

1. SSH into `~/ProductEngineerBench` (or your `--remote-repo` path) to clone if missing, fetch tags, checkout the requested ref, run `uv sync --frozen`, and ensure `/home/bench/results/<repo>/` exists.
2. Computes a deterministic run directory: `/home/bench/results/<repo>/<repo>_<UTC timestamp>`.
3. Invokes `scripts/run_benchmark.sh` with `--docker-context do-bench`, `--run-dir /home/bench/results/<repo>/<run_id>`, and `--print-run-dir`, forcing Docker to run on the droplet.
4. Exports the necessary env vars for the container (`BENCHMARK_IMAGE`, `SYNC_BUCKET`, `SYNC_PREFIX`, etc.).
5. Streams the container output locally and prints the final run directory + S3 destination.
6. Honors `--bench-ref` to pin the ProductEngineerBench revision for the run (default `main`).

Important flags/env vars:

| Flag/env | Description |
| --- | --- |
| `--repo NAME` | Name of the YAML file under `data/` (`NAME.yaml`). Mutually exclusive with `--config`. |
| `--config PATH` | Absolute path on the droplet to a specific YAML config. |
| `--bucket / $S3_BUCKET` | Bucket that stores run artifacts. Required for durability. |
| `--s3-prefix PREFIX` | Destination prefix for uploads (e.g., `remote/grand_central/<tag>`). |
| `--resume-prefix PREFIX` | Optional source prefix to download before running. Automatically sets `RESUME=1`. |
| `--remote-repo / $REMOTE_REPO_ROOT` | Path to ProductEngineerBench on the droplet (default `$HOME/ProductEngineerBench`). |
| `--remote-results / $REMOTE_RESULTS_ROOT` | Directory for run directories on the droplet (default `$HOME/results`). |
| `--env-file / $REMOTE_ENV_FILE` | Path to `.env.remote` on the droplet (default `$HOME/.env.remote`). |
| `--container-name` | Docker `--name` to make cancellation easier (default `peb-runner`). |
| `--skip-refresh` | Skip the SSH pull/sync step (useful when already running on the droplet). |
| `--cancel NAME` | Stop a running container via `docker --context <ctx> stop NAME`. |
| `--repo-url / $BENCH_REMOTE_REPO_URL` | Git URL to clone when bootstrapping the repo (defaults to your local `origin`). |
| `--bench-ref / $BENCH_REMOTE_REF` | Git ref (branch/tag/SHA) checked out before syncing (defaults to `main`). |

The script exports `BENCH_REMOTE=1`, so `scripts/run_benchmark.sh` avoids local path checks, prefers `.env.remote`, and prints `RUN_DIR=<abs-path>` for the caller.

**Secrets**: The helper passes `BENCHMARK_ENV_FILE=/home/bench/.env.remote` by default. When you run the helper from your laptop, ensure that file is available on the machine invoking Docker. The recommended workflow is to SSH into the droplet (or use a secure secret-sync mechanism) before running remote benchmarks so that `.env.remote` never leaves the host.

## 4. Durability & resume behaviour

Inside the container, the Python runner reacts to these env vars:

| Env var | Behavior |
| --- | --- |
| `SYNC_BUCKET` + `SYNC_PREFIX` | Enable S3 durability. On shutdown the runner executes `aws s3 sync $RUN_DIR s3://$SYNC_BUCKET/$SYNC_PREFIX --delete`. |
| `RESUME_PREFIX` | Before touching the repo, the runner executes `aws s3 sync s3://$SYNC_BUCKET/$RESUME_PREFIX $RUN_DIR` and enables resume mode. |
| `AWS_PROFILE` / `AWS_REGION` | Forwarded to `aws s3 sync`. The droplet mounts `/home/bench/.aws` into the container so credentials resolve automatically. |
| `RUN_ID` | Included in `manifest.json` to match the S3 prefix/run directory names. |

`RunState` now maintains `manifest.status`, `manifest.updated_at`, and `manifest.completed_at`. `success`, `failure`, or `cancelled` is written whenever the runner exits (including SIGTERM via `docker stop`). If the container is stopped mid-run, the S3 sync still runs (the signal handler raises `SystemExit`, ensuring the cleanup logic executes before Docker kills the process).

## 5. Fetching artifacts locally

Use the fetch helper to download artifacts from S3:

```bash
scripts/fetch_results.sh \
  --bucket peb-results-prod \
  --prefix remote/grand_central/2025-11-14T18-00Z \
  --dest ./results/remote-grand-central-20251114
```

The script validates that the prefix exists (via `aws s3api list-objects-v2`), runs `aws s3 sync`, and prints the local `manifest.json` path for inspection. Pass `--profile` or `--region` to override AWS defaults.

## 6. Cancellation

Runs launched via the helper use `--name peb-runner` (or your overridden `--container-name`). To stop a run and trigger the durability sync:

```bash
# Preferred: helper shortcut
scripts/run_remote_benchmark.sh --context do-bench --cancel peb-runner

# Manual alternative
docker --context do-bench stop peb-runner
```

Because the Python runner keeps a SIGTERM handler, `docker stop` marks the manifest as `cancelled` and still mirrors the run directory to S3 before exiting.

## 7. Local helper script improvements

`scripts/run_benchmark.sh` now accepts several quality-of-life flags that are useful locally and remotely:

| Flag | Description |
| --- | --- |
| `--repo NAME` / `--config PATH` | Run a single YAML file. |
| `--run-dir PATH` | Bypass the timestamp logic and use an explicit host results directory (single repo only). |
| `--print-run-dir` | Print `RUN_DIR=<abs-path>` so wrappers can capture the final directory. |
| `--docker-context NAME` | Use a Docker context (defaults to `default`). |
| `--container-name NAME` | Forward `--name` to `docker run` (suffixes `-<idx>` when iterating multiple repos). |

When `BENCH_REMOTE=1` or `--docker-context` is non-default, the script skips local path validation/mkdirs, prefers `.env.remote`, and leaves it to the remote daemon to create directories. It also forwards `SYNC_BUCKET`, `SYNC_PREFIX`, `RESUME_PREFIX`, `AWS_PROFILE`, etc., to the container and automatically passes `RUN_ID=$(basename $RUN_DIR)` so manifest IDs match your deterministic naming scheme.

Refer to this document whenever you need to wire up a new remote host or debug S3/resume behavior—the repo scripts are designed around the conventions captured here.
