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
  --config PATH           Local path to a specific repo config file (copied into run dir).
  --image IMAGE           Benchmark runner image (default: $BENCHMARK_IMAGE).
  --bucket NAME           S3 bucket for sync (default: $S3_BUCKET).
  --s3-prefix PREFIX      Destination prefix under the bucket (required).
  --resume-prefix PREFIX  Optional source prefix to resume from.
  --ssh-host HOST         Droplet hostname or IP (default: $BENCH_REMOTE_HOST).
  --ssh-user USER         SSH user (default: bench or $BENCH_REMOTE_USER).
  --remote-results PATH   Directory for run artifacts on the droplet (default: /home/bench/results).
  --env-file PATH         Local .env file to pass through Docker (default: ./\.env.remote or ./\.env).
  --container-name NAME   Name for docker --name (default: peb-runner).
  --stop-timeout SECONDS  Time docker stop waits before SIGKILL (default: 600).
  --cancel NAME           Stop an existing container via docker --context <NAME> stop.
  -h, --help              Show this message and exit.

Examples:
  scripts/run_remote_benchmark.sh \
    --context do-bench \
    --repo grand_central \
    --image registry.digitalocean.com/bench/runner:main \
    --bucket peb-results-prod \
    --s3-prefix remote/grand_central/2025-11-14T18-00Z

Behavior:
- Performs a single `/results` bind mount on the droplet. The selected repo YAML and `config/storymachine.yaml` are copied into that run directory before launch; the container reads them from `/results/data` and `/results/config`.
- `--ssh-host` is required so the helper can create/copy into the run directory on the droplet.
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
remote_results_root=${REMOTE_RESULTS_ROOT:-}
remote_env_file=${REMOTE_ENV_FILE:-}
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

if [[ -z "${remote_results_root}" ]]; then
    if [[ -z "${ssh_target}" ]]; then
        echo "Provide --ssh-host (or set REMOTE_RESULTS_ROOT) so remote paths can be resolved." >&2
        exit 1
    fi
    remote_home=$(ssh ${ssh_opts} "${ssh_target}" 'printf %s "$HOME"')
    remote_results_root=${remote_home}/results
fi

if [[ -z "${remote_env_file}" && -n "${local_env_file}" ]]; then
    remote_env_file="${local_env_file}"
fi

if [[ -n "${remote_env_file}" && "${remote_env_file}" != /* ]]; then
    remote_env_file="${REPO_ROOT}/${remote_env_file#./}"
fi

if [[ -n "${remote_env_file}" && ! -f "${remote_env_file}" ]]; then
    echo "--env-file must point to a local file accessible to the Docker client: ${remote_env_file}" >&2
    exit 1
fi

if [[ -n "${remote_env_file}" && ! -f "${remote_env_file}" ]]; then
    echo "--env-file must point to a local file; ${remote_env_file} not found on this machine." >&2
    exit 1
fi

if [[ -z "${remote_home}" && -n "${ssh_target}" ]]; then
    remote_home=$(ssh ${ssh_opts} "${ssh_target}" 'printf %s "$HOME"')
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

if [[ -n "${ssh_target}" ]]; then
    quoted_run_parent=$(printf %q "${run_parent}")
    quoted_run_dir=$(printf %q "${run_dir}")
    ssh ${ssh_opts} "${ssh_target}" "mkdir -p ${quoted_run_parent} ${quoted_run_dir}"
else
    echo "Warning: unable to pre-create run directory ${run_dir} (no --ssh-host provided)." >&2
fi

# Stage data/config into the run directory so the container can consume them from /results.
local_repo_config=""
if [[ -n "${repo_name}" ]]; then
    local_repo_config="${REPO_ROOT}/data/${repo_name}.yaml"
else
    # Allow relative paths from repo root
    if [[ "${config_override}" != /* ]]; then
        local_repo_config="${REPO_ROOT}/${config_override#./}"
    else
        local_repo_config="${config_override}"
    fi
fi

if [[ ! -f "${local_repo_config}" ]]; then
    echo "Missing repository config at ${local_repo_config}" >&2
    exit 1
fi

local_storymachine_config="${REPO_ROOT}/config/storymachine.yaml"
if [[ ! -f "${local_storymachine_config}" ]]; then
    echo "Missing storymachine config at ${local_storymachine_config}" >&2
    exit 1
fi

if [[ -z "${ssh_target}" ]]; then
    echo "Remote staging requires --ssh-host to copy data/config to the run directory." >&2
    exit 1
fi

remote_data_dir="${run_dir}/data"
remote_config_dir="${run_dir}/config"
ssh ${ssh_opts} "${ssh_target}" "mkdir -p $(printf %q "${remote_data_dir}") $(printf %q "${remote_config_dir}")"

# Copy repo config and supporting data (including PRD/tech-spec files) into the run dir.
scp ${ssh_opts} "${local_repo_config}" "${ssh_target}:$(printf %q "${remote_data_dir}/")"
scp ${ssh_opts} "${local_storymachine_config}" "${ssh_target}:$(printf %q "${remote_config_dir}/")"

local_repo_data_dir="${REPO_ROOT}/data/${repo_slug}"
if [[ -d "${local_repo_data_dir}" ]]; then
    scp -r ${ssh_opts} "${local_repo_data_dir}" "${ssh_target}:$(printf %q "${remote_data_dir}/")"
else
    echo "Warning: expected repo data directory ${local_repo_data_dir} not found; PRD/tech-spec may be missing." >&2
fi

env_args=(
    "DATA_DIR=/results/data"
    "CONFIG_DIR=/results/config"
    "RESULTS_DIR=${run_parent}"
    "BENCHMARK_IMAGE=${image_name}"
    "BENCHMARK_ENV_FILE=${remote_env_file}"
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
