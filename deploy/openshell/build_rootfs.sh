#!/usr/bin/env bash
# Build the E2E-05 stand-in rootfs tar for the OpenShell VM driver.
#
# Uses the documented rootfs-tar path: docker build -> docker create -> docker export.
# Requires a running Docker daemon (for example Colima). Removes the container and the
# local image it created; the output tar is written outside the repository.
#
#   deploy/openshell/build_rootfs.sh /abs/path/rfa-e2e05-rootfs.tar
set -euo pipefail
out="${1:?usage: build_rootfs.sh /abs/path/output.tar}"
case "$out" in /*) ;; *) echo "output path must be absolute" >&2; exit 2 ;; esac
here="$(cd "$(dirname "$0")" && pwd)"
tag="rfa-e2e05-rootfs:local"
docker build --quiet -t "$tag" -f "$here/image/Dockerfile" "$here/image" >/dev/null
cid="$(docker create "$tag")"
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true; docker rmi "$tag" >/dev/null 2>&1 || true' EXIT
docker export -o "$out" "$cid"
echo "rootfs=$out"
echo "rootfs_sha256=$(shasum -a 256 "$out" | cut -d' ' -f1)"
echo "rootfs_bytes=$(wc -c < "$out" | tr -d ' ')"
