#!/bin/sh
# CAO source bootstrap.
#
# Prepares a clone for use with no package manager and no remote installer
# script:
#
#   1. writes an idempotent CAO product configuration into the XDG config dir,
#      recording the pinned runtime dependency;
#   2. installs or adopts the pinned runtime binary through the stdlib-only
#      installer in ``src/cao/managed_herdr.py``, which verifies the SHA-256
#      before anything is put in place.
#
# This file is the single source of the pinned coordinates. The release asset
# is fetched only from the official GitHub release URL for the detected
# platform, with a timeout and a size cap. No ``curl``-to-shell path exists.
#
# Usage:
#   ./setup.sh                  adopt a compatible existing binary, else install
#   ./setup.sh --adopt-existing require using an existing compatible binary
#   ./setup.sh --download       always install a CAO-managed copy
#   ./setup.sh --no-download    verify only; fetch nothing (used by tests)
#   ./setup.sh --force          rewrite the managed fields in the config
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
python=${CAO_PYTHON:-python3}

# --- pinned managed-runtime coordinates (official release only) -------------
RUNTIME_ID=herdr
MANAGED_NAME=herdr
MANAGED_VERSION=0.9.3
MANAGED_SIZE_CAP_BYTES=268435456
MANAGED_TIMEOUT_SECONDS=60
MANAGED_ALLOWED_HOSTS='["github.com"]'
MANAGED_PROBE_ARGV='["--version"]'
URL_LINUX_X86_64=https://github.com/herdrdev/herdr/releases/download/v0.9.3/herdr-linux-x86_64
SHA_LINUX_X86_64=18a8dc65f1c2fa485884344356dea1cfd911c6f06cf46fa78e193f4087f4dba7
URL_LINUX_AARCH64=https://github.com/herdrdev/herdr/releases/download/v0.9.3/herdr-linux-aarch64
SHA_LINUX_AARCH64=4de7aa3e25678812e92960de64f7c2aaa1bca1f0f80a3c5e559837e231e1f5c0

POLICY=auto
FORCE=

usage() {
    sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --adopt-existing) POLICY=adopt ;;
        --download) POLICY=managed ;;
        --no-download|--check) POLICY=verify ;;
        --force) FORCE=--force ;;
        -h|--help) usage; exit 0 ;;
        *)
            echo "setup.sh: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

CAO_MANAGED_SPEC=$(
    cat <<JSON
{"name":"$MANAGED_NAME","version":"$MANAGED_VERSION","size_cap_bytes":$MANAGED_SIZE_CAP_BYTES,"timeout_seconds":$MANAGED_TIMEOUT_SECONDS,"allowed_hosts":$MANAGED_ALLOWED_HOSTS,"probe_argv":$MANAGED_PROBE_ARGV,"platforms":{"linux-x86_64":{"filename":"herdr-linux-x86_64","url":"$URL_LINUX_X86_64","sha256":"$SHA_LINUX_X86_64"},"linux-aarch64":{"filename":"herdr-linux-aarch64","url":"$URL_LINUX_AARCH64","sha256":"$SHA_LINUX_AARCH64"}}}
JSON
)
export CAO_MANAGED_SPEC
export CAO_RUNTIME_ID="$RUNTIME_ID"

if [ -z "${PYTHONPATH:-}" ]; then
    PYTHONPATH="$here/src"
else
    PYTHONPATH="$here/src:$PYTHONPATH"
fi
export PYTHONPATH

# shellcheck disable=SC2086  # FORCE is an optional, pre-split flag
"$python" -B -S -m cao.bootstrap install --policy "$POLICY" $FORCE
