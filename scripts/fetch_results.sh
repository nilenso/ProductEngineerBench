#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/fetch_results.sh --bucket BUCKET --prefix PREFIX --dest PATH [options]

Download a durable run directory from S3, grab the matching tarball, and
extract it locally (unless --no-extract is set).

Required:
  --bucket NAME      S3 bucket storing run artifacts.
  --prefix PREFIX    Prefix under the bucket (e.g. remote/grand_central/run_123).
  --dest PATH        Local destination directory (created if missing).

Optional:
  --profile NAME     AWS profile to use (overrides AWS_PROFILE).
  --region NAME      AWS region to use (overrides AWS_REGION).
  --no-extract       Download the tarball but do not extract it.
  --dry-run          Print intended commands without hitting AWS.
  -h, --help         Show this message and exit.
EOF
}

bucket=""
prefix=""
dest=""
profile=${AWS_PROFILE:-}
region=${AWS_REGION:-}
dry_run=0
no_extract=0

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
        --no-extract)
            no_extract=1
            shift
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

log() {
    printf "[fetch] %s\n" "$*"
}

run_or_print() {
    if (( dry_run )); then
        printf "(dry-run) "
        printf "%q " "$@"
        printf "\n"
    else
        "$@"
    fi
}

aws_args=()
if [[ -n "${profile}" ]]; then
    aws_args+=(--profile "${profile}")
fi
if [[ -n "${region}" ]]; then
    aws_args+=(--region "${region}")
fi

dest_dir=${dest%/}
prefix_clean=${prefix%/}
s3_prefix_uri="s3://${bucket}/${prefix_clean}"
tar_key="${prefix_clean}.tar.gz"
tar_uri="s3://${bucket}/${tar_key}"
local_tar="${dest_dir}/$(basename "${tar_key}")"

mkdir -p "${dest_dir}"

validate_prefix() {
    if (( dry_run )); then
        log "Skipping prefix validation (dry-run)."
        return
    fi
    log "Validating ${s3_prefix_uri} exists..."
    if ! first_key=$(aws "${aws_args[@]}" s3api list-objects-v2 \
        --bucket "${bucket}" \
        --prefix "${prefix_clean}" \
        --max-keys 1 \
        --no-paginate \
        --query 'Contents[0].Key' \
        --output text 2>/dev/null); then
        echo "Failed to list objects under ${s3_prefix_uri}." >&2
        exit 1
    fi
    if [[ -z "${first_key}" || "${first_key}" == "None" ]]; then
        echo "No objects found under ${s3_prefix_uri}." >&2
        exit 1
    fi
}

sync_prefix() {
    log "Syncing ${s3_prefix_uri} -> ${dest_dir}"
    run_or_print aws "${aws_args[@]}" s3 sync "${s3_prefix_uri}" "${dest_dir}"
}

download_tarball() {
    if (( dry_run )); then
        log "Would download tarball ${tar_uri} -> ${local_tar}"
        return 0
    fi

    log "Checking for tarball ${tar_uri}..."
    if ! aws "${aws_args[@]}" s3api head-object --bucket "${bucket}" --key "${tar_key}" >/dev/null 2>&1; then
        log "Tarball not found; skipping download (sync results may still be usable)."
        return 1
    fi

    log "Downloading tarball to ${local_tar}"
    run_or_print aws "${aws_args[@]}" s3 cp "${tar_uri}" "${local_tar}"
    return 0
}

extract_tarball() {
    if (( no_extract )); then
        log "--no-extract set; leaving tarball at ${local_tar}"
        return
    fi
    if (( dry_run )); then
        log "Would extract ${local_tar} into ${dest_dir}"
        return
    fi
    if [[ ! -f "${local_tar}" ]]; then
        log "Tarball ${local_tar} not present; skipping extract."
        return
    fi
    log "Extracting tarball into ${dest_dir}"
    tar -xzf "${local_tar}" -C "${dest_dir}"
}

validate_prefix
sync_prefix
if download_tarball; then
    extract_tarball
else
    log "Proceeding without tarball extraction."
fi

manifest_path="${dest_dir}/manifest.json"
if [[ -f "${manifest_path}" ]]; then
    log "Fetched manifest at ${manifest_path}"
else
    log "manifest.json not found under ${dest_dir}; partial download?"
fi
