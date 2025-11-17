#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

usage() {
    cat <<'EOF'
Usage: scripts/run_remote_benchmark.sh [options]

Options:
  --context NAME          Docker context to use (default: do-bench or $BENCH_REMOTE_CONTEXT)
  --repo NAME             Repository slug from data/NAME.yaml (mutually exclusive with --config).
  --config PATH           Remote path to a specific repo config file.
  --image IMAGE           Benchmark runner image (default: $BENCHMARK_IMAGE).
  --bucket NAME           S3 bucket for sync (default: $S3_BUCKET).
  --s3-prefix PREFIX      Destination prefix under the bucket (required).
  --resume-prefix PREFIX  Optional source prefix to resume from.
  --ssh-host HOST         Droplet hostname or IP (default: $BENCH_REMOTE_HOST).
  --ssh-user USER         SSH user (default: bench or $BENCH_REMOTE_USER).
  --remote-repo PATH      Path to ProductEngineerBench on the droplet (default: /home/bench/ProductEngineerBench).
  --repo-url URL          Git URL used when cloning the repo remotely (default: current origin).
  --bench-ref REF         Git ref (branch/tag/SHA) to checkout before syncing (default: main or $BENCH_REMOTE_REF).
  --remote-results PATH   Directory for run artifacts on the droplet (default: /home/bench/results).
  --env-file PATH         Remote .env path (default: /home/bench/.env.remote).
  --container-name NAME   Name for docker --name (default: peb-runner).
  --stop-timeout SECONDS  Time docker stop waits before SIGKILL (default: 600).
  --skip-refresh          Skip the git pull / uv sync step on the droplet.
  --cancel NAME           Stop an existing container via docker --context <NAME> stop.
  -h, --help              Show this message and exit.

Examples:
  scripts/run_remote_benchmark.sh \
    --context do-bench \
    --repo grand_central \
    --image registry.digitalocean.com/bench/runner:main \
    --bucket peb-results-prod \
    --s3-prefix remote/grand_central/2025-11-14T18-00Z
EOF
}

context_name=${BENCH_REMOTE_CONTEXT:-do-bench}
repo_name=""
config_override=""
image_name=${BENCHMARK_IMAGE:-}
bucket=${S3_BUCKET:-}
s3_prefix=""
resume_prefix=""
ssh_host=${BENCH_REMOTE_HOST:-}
ssh_user=${BENCH_REMOTE_USER:-bench}
ssh_opts=${BENCH_REMOTE_SSH_OPTS:-}
remote_repo_root=${REMOTE_REPO_ROOT:-}
remote_results_root=${REMOTE_RESULTS_ROOT:-}
remote_env_file=${REMOTE_ENV_FILE:-}
remote_repo_url=${BENCH_REMOTE_REPO_URL:-}
bench_ref=${BENCH_REMOTE_REF:-main}
skip_refresh=0
container_name="peb-runner"
cancel_target=""
remote_home=""
stop_timeout=${BENCH_REMOTE_STOP_TIMEOUT:-600}
local_env_file=""
if [[ -f "${REPO_ROOT}/.env.remote" ]]; then
    local_env_file="${REPO_ROOT}/.env.remote"
elif [[ -f "${REPO_ROOT}/.env" ]]; then
    local_env_file="${REPO_ROOT}/.env"
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --context)
            [[ $# -ge 2 ]] || { echo "--context requires an argument" >&2; exit 1; }
            context_name="$2"
            shift 2
            ;;
        --repo)
            [[ $# -ge 2 ]] || { echo "--repo requires an argument" >&2; exit 1; }
            repo_name="$2"
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || { echo "--config requires an argument" >&2; exit 1; }
            config_override="$2"
            shift 2
            ;;
        --image)
            [[ $# -ge 2 ]] || { echo "--image requires an argument" >&2; exit 1; }
            image_name="$2"
            shift 2
            ;;
        --bucket)
            [[ $# -ge 2 ]] || { echo "--bucket requires an argument" >&2; exit 1; }
            bucket="$2"
            shift 2
            ;;
        --s3-prefix)
            [[ $# -ge 2 ]] || { echo "--s3-prefix requires an argument" >&2; exit 1; }
            s3_prefix="$2"
            shift 2
            ;;
        --resume-prefix)
            [[ $# -ge 2 ]] || { echo "--resume-prefix requires an argument" >&2; exit 1; }
            resume_prefix="$2"
            shift 2
            ;;
        --ssh-host)
            [[ $# -ge 2 ]] || { echo "--ssh-host requires an argument" >&2; exit 1; }
            ssh_host="$2"
            shift 2
            ;;
        --ssh-user)
            [[ $# -ge 2 ]] || { echo "--ssh-user requires an argument" >&2; exit 1; }
            ssh_user="$2"
            shift 2
            ;;
        --remote-repo)
            [[ $# -ge 2 ]] || { echo "--remote-repo requires an argument" >&2; exit 1; }
            remote_repo_root="$2"
            shift 2
            ;;
        --remote-results)
            [[ $# -ge 2 ]] || { echo "--remote-results requires an argument" >&2; exit 1; }
            remote_results_root="$2"
            shift 2
            ;;
        --env-file)
            [[ $# -ge 2 ]] || { echo "--env-file requires an argument" >&2; exit 1; }
            remote_env_file="$2"
            shift 2
            ;;
        --repo-url)
            [[ $# -ge 2 ]] || { echo "--repo-url requires an argument" >&2; exit 1; }
            remote_repo_url="$2"
            shift 2
            ;;
        --bench-ref)
            [[ $# -ge 2 ]] || { echo "--bench-ref requires an argument" >&2; exit 1; }
            bench_ref="$2"
            shift 2
            ;;
        --container-name)
            [[ $# -ge 2 ]] || { echo "--container-name requires an argument" >&2; exit 1; }
            container_name="$2"
            shift 2
            ;;
        --stop-timeout)
            [[ $# -ge 2 ]] || { echo "--stop-timeout requires an argument" >&2; exit 1; }
            if ! [[ "$2" =~ ^[0-9]+$ ]]; then
                echo "--stop-timeout expects an integer number of seconds" >&2
                exit 1
            fi
            stop_timeout="$2"
            shift 2
            ;;
        --skip-refresh)
            skip_refresh=1
            shift
            ;;
        --cancel)
            [[ $# -ge 2 ]] || { echo "--cancel requires an argument" >&2; exit 1; }
            cancel_target="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            exit 1
            ;;
    esac
done

docker_cmd=(docker --context "${context_name}")

if [[ -n "${cancel_target}" ]]; then
    echo "Stopping container ${cancel_target} via docker --context ${context_name} stop --time ${stop_timeout}..."
    echo "Waiting up to ${stop_timeout}s so the runner can upload its results to S3."
    "${docker_cmd[@]}" stop --time "${stop_timeout}" "${cancel_target}"
    exit 0
fi

if [[ -z "${repo_name}" && -z "${config_override}" ]]; then
    echo "Specify --repo or --config to choose a benchmark configuration." >&2
    exit 1
fi

if [[ -n "${repo_name}" && -n "${config_override}" ]]; then
    echo "--repo and --config are mutually exclusive." >&2
    exit 1
fi

if [[ -z "${image_name}" ]]; then
    echo "Provide --image or set BENCHMARK_IMAGE to reference the remote runner image." >&2
    exit 1
fi

if [[ -z "${bucket}" ]]; then
    echo "Provide --bucket or set S3_BUCKET to enable durability sync." >&2
    exit 1
fi

if [[ -z "${s3_prefix}" ]]; then
    echo "--s3-prefix is required to describe where run artifacts should be written." >&2
    exit 1
fi

ssh_target=""
if [[ -n "${ssh_host}" ]]; then
    ssh_target="${ssh_user}@${ssh_host}"
fi

if [[ -z "${remote_repo_root}" || -z "${remote_results_root}" ]]; then
    if [[ -z "${ssh_target}" ]]; then
        echo "Provide --ssh-host (or set REMOTE_REPO_ROOT/REMOTE_RESULTS_ROOT) so remote paths can be resolved." >&2
        exit 1
    fi
    remote_home=$(ssh ${ssh_opts} "${ssh_target}" 'printf %s "$HOME"')
    remote_repo_root=${remote_repo_root:-${remote_home}/ProductEngineerBench}
    remote_results_root=${remote_results_root:-${remote_home}/results}
fi

if [[ -z "${remote_env_file}" && -n "${local_env_file}" ]]; then
    remote_env_file="${local_env_file}"
fi

if [[ -n "${remote_env_file}" && "${remote_env_file}" != /* ]]; then
    remote_env_file="${REPO_ROOT}/${remote_env_file#./}"
fi

if [[ -z "${remote_home}" && -n "${ssh_target}" ]]; then
    remote_home=$(ssh ${ssh_opts} "${ssh_target}" 'printf %s "$HOME"')
fi

if [[ -z "${remote_repo_url}" ]]; then
    if origin_url=$(git -C "${REPO_ROOT}" config --get remote.origin.url 2>/dev/null); then
        remote_repo_url="${origin_url}"
    fi
fi
if [[ -z "${remote_repo_url}" ]]; then
    remote_repo_url="https://github.com/nilenso/ProductEngineerBench.git"
fi

repo_slug="${repo_name}"
if [[ -z "${repo_slug}" ]]; then
    repo_slug=$(basename -- "${config_override}")
    repo_slug="${repo_slug%.yaml}"
fi

timestamp=$(date -u +%Y%m%d_%H%M%SZ)
run_id="${repo_slug}_${timestamp}"
remote_results_root=${remote_results_root%/}
run_parent="${remote_results_root}/${repo_slug}"
run_dir="${run_parent}/${run_id}"

if (( ! skip_refresh )); then
    if [[ -z "${ssh_target}" ]]; then
        echo "Set BENCH_REMOTE_HOST or pass --ssh-host so the repository can be refreshed." >&2
        exit 1
    fi

    printf "Refreshing %s on %s (ref=%s)...\n" "${remote_repo_root}" "${ssh_target}" "${bench_ref}"
    quoted_repo_root=$(printf '%q' "${remote_repo_root}")
    quoted_repo_url=$(printf '%q' "${remote_repo_url}")
    quoted_bench_ref=$(printf '%q' "${bench_ref}")
    quoted_run_parent=$(printf '%q' "${run_parent}")
    remote_cmd=$(cat <<EOF
set -euo pipefail
REPO_DIR=${quoted_repo_root}
REPO_URL=${quoted_repo_url}
BENCH_REF=${quoted_bench_ref}
RUN_PARENT=${quoted_run_parent}
PARENT_DIR=\$(dirname "\$REPO_DIR")
mkdir -p "\$PARENT_DIR"
if [[ ! -d "\$REPO_DIR/.git" ]]; then
    echo "Cloning ProductEngineerBench into \$REPO_DIR"
    rm -rf "\$REPO_DIR"
    git clone "\$REPO_URL" "\$REPO_DIR"
fi
cd "\$REPO_DIR"
git fetch origin --tags --prune
git fetch origin "\$BENCH_REF" || true
if git rev-parse --verify --quiet "\$BENCH_REF"; then
    git checkout --detach "\$BENCH_REF"
elif git rev-parse --verify --quiet "origin/\$BENCH_REF"; then
    git checkout --detach "origin/\$BENCH_REF"
else
    git checkout --detach "\$BENCH_REF"
fi
uv sync --frozen
mkdir -p "\$RUN_PARENT"
EOF
)
    ssh ${ssh_opts} "${ssh_target}" "bash -lc $(printf '%q' "${remote_cmd}")"
fi

if [[ -n "${ssh_target}" ]]; then
    quoted_run_dir=$(printf %q "${run_dir}")
    ssh ${ssh_opts} "${ssh_target}" "mkdir -p ${quoted_run_dir}"
else
    echo "Warning: unable to pre-create run directory ${run_dir} (no --ssh-host provided)." >&2
fi

env_args=(
    "DATA_DIR=${remote_repo_root}/data"
    "CONFIG_DIR=${remote_repo_root}/config"
    "RESULTS_DIR=${run_parent}"
    "BENCHMARK_IMAGE=${image_name}"
    "BENCHMARK_ENV_FILE=${remote_env_file}"
    "BENCH_REMOTE=1"
    "SYNC_BUCKET=${bucket}"
    "SYNC_PREFIX=${s3_prefix}"
)

if [[ -n "${resume_prefix}" ]]; then
    env_args+=("RESUME_PREFIX=${resume_prefix}" "RESUME=1")
fi

if [[ -n "${remote_home}" ]]; then
    env_args+=("BENCH_AWS_DIR=${remote_home%/}/.aws")
fi

benchmark_args=(--docker-context "${context_name}" --print-run-dir --run-dir "${run_dir}" --container-name "${container_name}")
if [[ -n "${repo_name}" ]]; then
    benchmark_args+=(--repo "${repo_name}")
else
    benchmark_args+=(--config "${config_override}")
fi

tmp_log=$(mktemp -t peb-remote-run.XXXXXX)
trap 'rm -f "${tmp_log}"' EXIT

printf "Launching remote benchmark (run_id=%s) via docker context %s...\n" "${run_id}" "${context_name}"

set +e
(
    cd "${REPO_ROOT}"
    env "${env_args[@]}" "${SCRIPT_DIR}/run_benchmark.sh" "${benchmark_args[@]}"
) 2>&1 | tee "${tmp_log}"
status=${PIPESTATUS[0]}
set -e

if (( status != 0 )); then
    echo "Benchmark exited with status ${status}."
    exit "${status}"
fi

printed_run_dir=$(grep -E '^RUN_DIR=' "${tmp_log}" | tail -n 1 | cut -d= -f2-)
effective_run_dir=${printed_run_dir:-${run_dir}}

cat <<EOF
Remote benchmark completed.
  Host run dir : ${effective_run_dir}
  Run ID       : ${run_id}
  S3 location  : s3://${bucket}/${s3_prefix}
EOF

if [[ -n "${resume_prefix}" ]]; then
    echo "  Resume data : s3://${bucket}/${resume_prefix}"
fi
