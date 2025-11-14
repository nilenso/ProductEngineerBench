#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

usage() {
    cat <<'EOF'
Usage: scripts/setup_docker_context.sh [options]

Options:
  --context NAME        Docker context name (default: do-bench or $BENCH_REMOTE_CONTEXT)
  --host HOST           Remote host (default: $BENCH_REMOTE_HOST)
  --user USER           SSH user (default: bench or $BENCH_REMOTE_USER)
  --apply               Execute the docker context create command instead of printing it.
  --force               Remove any existing context before creating a new one.
  -h, --help            Show this message and exit.

The script prints the docker context command using ssh://USER@HOST. Use --apply to
run it automatically after confirming the values.
EOF
}

context_name=${BENCH_REMOTE_CONTEXT:-do-bench}
remote_host=${BENCH_REMOTE_HOST:-}
remote_user=${BENCH_REMOTE_USER:-bench}
apply=0
force=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --context)
            [[ $# -ge 2 ]] || { echo "--context requires an argument" >&2; exit 1; }
            context_name="$2"
            shift 2
            ;;
        --host)
            [[ $# -ge 2 ]] || { echo "--host requires an argument" >&2; exit 1; }
            remote_host="$2"
            shift 2
            ;;
        --user)
            [[ $# -ge 2 ]] || { echo "--user requires an argument" >&2; exit 1; }
            remote_user="$2"
            shift 2
            ;;
        --apply)
            apply=1
            shift
            ;;
        --force)
            force=1
            shift
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

if [[ -z "${remote_host}" ]]; then
    echo "Set BENCH_REMOTE_HOST or pass --host to identify the target droplet." >&2
    exit 1
fi

ssh_target="${remote_user}@${remote_host}"
context_cmd=(
    docker context create "${context_name}"
    "--description" "ProductEngineerBench remote runner"
    "--docker" "host=ssh://${ssh_target}"
)

printf "Docker context command:\n  "
printf "%q " "${context_cmd[@]}"
printf "\n"

if (( ! apply )); then
    cat <<'EOF'
(dry-run) Not creating the context. Re-run with --apply once you are satisfied with the values.
EOF
    exit 0
fi

if (( force )); then
    if docker context inspect "${context_name}" >/dev/null 2>&1; then
        docker context rm -f "${context_name}" >/dev/null
    fi
elif docker context inspect "${context_name}" >/dev/null 2>&1; then
    echo "Context ${context_name} already exists. Use --force to replace it." >&2
    exit 1
fi

"${context_cmd[@]}"
docker context use "${context_name}" >/dev/null 2>&1 || true
echo "Context ${context_name} created. Use 'docker --context ${context_name} ps' to verify connectivity."
