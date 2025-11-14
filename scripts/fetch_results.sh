#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

usage() {
    cat <<'EOF'
Usage: scripts/fetch_results.sh --bucket BUCKET --prefix PREFIX --dest PATH [options]

Options:
  --bucket NAME      S3 bucket that stores benchmark artifacts.
  --prefix PREFIX    Prefix under the bucket (e.g. remote/grand_central/run_123).
  --dest PATH        Local destination directory (created if missing).
  --profile NAME     AWS profile to use (overrides AWS_PROFILE).
  --region NAME      AWS region to use (overrides AWS_REGION).
  --dry-run          Print commands without executing aws s3 sync.
  -h, --help         Show this message and exit.
EOF
}

bucket=""
prefix=""
dest=""
profile=${AWS_PROFILE:-}
region=${AWS_REGION:-}
dry_run=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bucket)
            [[ $# -ge 2 ]] || { echo "--bucket requires an argument" >&2; exit 1; }
            bucket="$2"
            shift 2
            ;;
        --prefix)
            [[ $# -ge 2 ]] || { echo "--prefix requires an argument" >&2; exit 1; }
            prefix="$2"
            shift 2
            ;;
        --dest)
            [[ $# -ge 2 ]] || { echo "--dest requires an argument" >&2; exit 1; }
            dest="$2"
            shift 2
            ;;
        --profile)
            [[ $# -ge 2 ]] || { echo "--profile requires an argument" >&2; exit 1; }
            profile="$2"
            shift 2
            ;;
        --region)
            [[ $# -ge 2 ]] || { echo "--region requires an argument" >&2; exit 1; }
            region="$2"
            shift 2
            ;;
        --dry-run)
            dry_run=1
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

if [[ -z "${bucket}" || -z "${prefix}" || -z "${dest}" ]]; then
    echo "--bucket, --prefix, and --dest are required." >&2
    usage
    exit 1
fi

s3_uri="s3://${bucket}/${prefix}"
aws_args=()
aws_api_args=()
if [[ -n "${profile}" ]]; then
    aws_args+=(--profile "${profile}")
    aws_api_args+=(--profile "${profile}")
fi
if [[ -n "${region}" ]]; then
    aws_args+=(--region "${region}")
    aws_api_args+=(--region "${region}")
fi

echo "Validating ${s3_uri}..."
metadata=$(aws "${aws_api_args[@]}" s3api list-objects-v2 --bucket "${bucket}" --prefix "${prefix}" --max-items 1)
key_count=$(printf "%s" "${metadata}" | python3 - <<'PY'
import json, sys
try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError:
    print(0)
    sys.exit(0)
print(payload.get("KeyCount", 0))
PY
)
if [[ "${key_count}" == "0" ]]; then
    echo "No objects found under ${s3_uri}." >&2
    exit 1
fi

mkdir -p "${dest}"
sync_cmd=(aws "${aws_args[@]}" s3 sync "${s3_uri}" "${dest}")

if (( dry_run )); then
    printf "(dry-run) "
    printf "%q " "${sync_cmd[@]}"
    printf "\n"
    exit 0
fi

"${sync_cmd[@]}"
manifest_path="${dest%/}/manifest.json"
if [[ -f "${manifest_path}" ]]; then
    echo "Fetched manifest: ${manifest_path}"
else
    echo "Sync complete. manifest.json not found under ${dest}."
fi
