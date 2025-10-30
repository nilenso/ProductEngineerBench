#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

DATA_DIR=${DATA_DIR:-"${REPO_ROOT}/data"}
CONFIG_DIR=${CONFIG_DIR:-"${REPO_ROOT}/config"}
RESULTS_ROOT=${RESULTS_DIR:-"${REPO_ROOT}/results"}
ENV_FILE=${BENCHMARK_ENV_FILE:-"${REPO_ROOT}/.env"}
IMAGE_NAME=${BENCHMARK_IMAGE:-"benchmark-runner"}

# Ensure required directories and files exist
if [[ ! -d "${DATA_DIR}" ]]; then
    echo "Expected data directory at ${DATA_DIR}" >&2
    exit 1
fi

if [[ ! -d "${CONFIG_DIR}" ]]; then
    echo "Expected config directory at ${CONFIG_DIR}" >&2
    exit 1
fi

mkdir -p "${RESULTS_ROOT}"

# Discover repo configurations
shopt -s nullglob
repo_files=("${DATA_DIR}"/*.yaml)
shopt -u nullglob

if (( ${#repo_files[@]} == 0 )); then
    echo "No repository configuration files found in ${DATA_DIR}" >&2
    exit 0
fi

for repo_data in "${repo_files[@]}"; do
    repo_rel="${repo_data#"${DATA_DIR}/"}"
    repo_name="${repo_rel%.yaml}"
    timestamp=$(date +%Y%m%d_%H%M%S)
    run_dir="${RESULTS_ROOT}/${repo_name}_${timestamp}"
    mkdir -p "${run_dir}"

    echo "\n=== Running benchmark for ${repo_name} (results -> ${run_dir}) ==="

    docker_args=(
        "--rm"
        "-v" "${DATA_DIR}:/data:ro"
        "-v" "${CONFIG_DIR}:/config:ro"
        "-v" "${run_dir}:/results"
        "-e" "REPO_CONFIG=/data/${repo_rel}"
        "-e" "STORYMACHINE_CONFIG=/config/storymachine.yaml"
        "-e" "RESULTS_DIR=/results"
    )

    if [[ -n "${GIT_TOKEN:-}" ]]; then
        docker_args+=("-e" "GIT_TOKEN=${GIT_TOKEN}")
    fi

    if [[ -f "${ENV_FILE}" ]]; then
        docker_args+=("--env-file" "${ENV_FILE}")
    fi

    docker run "${docker_args[@]}" "${IMAGE_NAME}"
done
