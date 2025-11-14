#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

usage() {
    cat <<'EOF'
Usage: scripts/run_benchmark.sh [options]

Options:
  --repo NAME            Run only data/NAME.yaml.
  --config PATH          Run a specific repo config file (repeatable).
  --run-dir PATH         Use a deterministic host results directory (single repo).
  --print-run-dir        Emit RUN_DIR=<path> for the resolved run directory.
  --docker-context NAME  Use the provided docker context (default: default).
  --container-name NAME  Set docker --name for the benchmark container.
  -h, --help             Show this message and exit.
EOF
}

DATA_DIR=${DATA_DIR:-"${REPO_ROOT}/data"}
CONFIG_DIR=${CONFIG_DIR:-"${REPO_ROOT}/config"}
RESULTS_ROOT=${RESULTS_DIR:-"${REPO_ROOT}/results"}
IMAGE_NAME=${BENCHMARK_IMAGE:-"benchmark-runner"}
docker_context="default"
container_name=${BENCHMARK_CONTAINER_NAME:-}
run_dir_override=${RUN_DIR:-}
print_run_dir=0
declare -a selected_repos=()
declare -a config_overrides=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)
            [[ $# -ge 2 ]] || { echo "--repo requires an argument" >&2; exit 1; }
            selected_repos+=("$2")
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || { echo "--config requires an argument" >&2; exit 1; }
            config_overrides+=("$2")
            shift 2
            ;;
        --run-dir)
            [[ $# -ge 2 ]] || { echo "--run-dir requires an argument" >&2; exit 1; }
            run_dir_override="$2"
            shift 2
            ;;
        --print-run-dir)
            print_run_dir=1
            shift
            ;;
        --docker-context)
            [[ $# -ge 2 ]] || { echo "--docker-context requires an argument" >&2; exit 1; }
            docker_context="$2"
            shift 2
            ;;
        --container-name)
            [[ $# -ge 2 ]] || { echo "--container-name requires an argument" >&2; exit 1; }
            container_name="$2"
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

BENCH_REMOTE=${BENCH_REMOTE:-0}
remote_mode=0
if [[ "${docker_context}" != "default" ]] || [[ "${BENCH_REMOTE}" == "1" ]]; then
    remote_mode=1
fi

env_file=${BENCHMARK_ENV_FILE:-}
if [[ -z "${env_file}" ]]; then
    if (( remote_mode )) && [[ -f "${REPO_ROOT}/.env.remote" ]]; then
        env_file="${REPO_ROOT}/.env.remote"
    elif [[ -f "${REPO_ROOT}/.env" ]]; then
        env_file="${REPO_ROOT}/.env"
    fi
fi

manage_local_paths=1
if (( remote_mode )); then
    manage_local_paths=0
fi

if (( manage_local_paths )); then
    if [[ ! -d "${DATA_DIR}" ]]; then
        echo "Expected data directory at ${DATA_DIR}" >&2
        exit 1
    fi
    if [[ ! -d "${CONFIG_DIR}" ]]; then
        echo "Expected config directory at ${CONFIG_DIR}" >&2
        exit 1
    fi
    mkdir -p "${RESULTS_ROOT}"
fi

resolve_config_path() {
    local path="$1"
    if [[ "${path}" == /* ]]; then
        printf "%s" "${path}"
    else
        printf "%s/%s" "${REPO_ROOT}" "${path#./}"
    fi
}

declare -a repo_files=()

if (( ${#config_overrides[@]} > 0 )); then
    for cfg in "${config_overrides[@]}"; do
        repo_files+=("$(resolve_config_path "${cfg}")")
    done
elif (( ${#selected_repos[@]} > 0 )); then
    for repo in "${selected_repos[@]}"; do
        repo_files+=("${DATA_DIR%/}/${repo}.yaml")
    done
else
    if (( remote_mode )); then
        echo "Remote context requires --repo or --config so paths can be determined explicitly." >&2
        exit 1
    fi
    shopt -s nullglob
    repo_files=("${DATA_DIR}"/*.yaml)
    shopt -u nullglob
fi

if (( ${#repo_files[@]} == 0 )); then
    echo "No repository configuration files found." >&2
    exit 0
fi

if [[ -n "${run_dir_override}" ]] && (( ${#repo_files[@]} != 1 )); then
    echo "--run-dir requires exactly one --repo/--config selection." >&2
    exit 1
fi

if [[ -n "${run_dir_override}" ]] && [[ "${run_dir_override}" != /* ]]; then
    echo "--run-dir expects an absolute path." >&2
    exit 1
fi

docker_cmd=(docker)
if [[ "${docker_context}" != "default" ]]; then
    docker_cmd+=(--context "${docker_context}")
fi

pass_env_vars=(
    GIT_TOKEN
    GIT_USERNAME
    RESUME
    FORCE_RESUME
    FORCE_RETRY_FAILED
    SKIP_GENERATE_STORIES
    SKIP_IMPLEMENT
    SKIP_EVALUATE
    EVALUATE_IF_RESULT_PRESENT
    SYNC_BUCKET
    SYNC_PREFIX
    RESUME_PREFIX
    AWS_PROFILE
    AWS_REGION
    AWS_DEFAULT_REGION
)

run_index=0
for repo_data in "${repo_files[@]}"; do
    run_index=$((run_index + 1))
    repo_filename=$(basename -- "${repo_data}")
    repo_name="${repo_filename%.yaml}"
    data_mount_source=$(dirname -- "${repo_data}")
    container_data_root="/data"

    if (( manage_local_paths )) && [[ ! -f "${repo_data}" ]]; then
        echo "Missing repository config ${repo_data}" >&2
        exit 1
    fi

    if [[ -z "${run_dir_override}" ]]; then
        timestamp=$(date +%Y%m%d_%H%M%S)
        run_dir="${RESULTS_ROOT%/}/${repo_name}_${timestamp}"
    else
        run_dir="${run_dir_override}"
    fi

    if (( manage_local_paths )); then
        mkdir -p "${run_dir}"
    fi

    if (( print_run_dir )); then
        printf "RUN_DIR=%s\n" "${run_dir}"
    fi

    printf "\n=== Running benchmark for %s (results -> %s) ===\n" "${repo_name}" "${run_dir}"

    docker_args=(
        "--rm"
        "-v" "${data_mount_source}:${container_data_root}:ro"
        "-v" "${CONFIG_DIR}:/config:ro"
        "-v" "${run_dir}:/results"
        "-e" "REPO_CONFIG=${container_data_root}/${repo_filename}"
        "-e" "STORYMACHINE_CONFIG=/config/storymachine.yaml"
        "-e" "RESULTS_DIR=/results"
        "-e" "RUN_DIR=/results"
        "-e" "RUN_ID=$(basename "${run_dir}")"
    )

    if [[ -n "${container_name}" ]]; then
        unique_name="${container_name}"
        if (( ${#repo_files[@]} > 1 )); then
            unique_name="${container_name}-${run_index}"
        fi
        docker_args+=("--name" "${unique_name}")
    fi

    for var in "${pass_env_vars[@]}"; do
        if [[ -n "${!var:-}" ]]; then
            docker_args+=("-e" "${var}=${!var}")
        fi
    done

    if [[ -n "${env_file}" && -f "${env_file}" ]]; then
        docker_args+=("--env-file" "${env_file}")
    fi

    if [[ -d "${HOME}/.aws" ]]; then
        docker_args+=("-v" "${HOME}/.aws:/root/.aws:ro")
    fi

    set +e
    "${docker_cmd[@]}" run "${docker_args[@]}" "${IMAGE_NAME}"
    exit_code=$?
    set -e

    if (( exit_code != 0 )); then
        exit "${exit_code}"
    fi
done
