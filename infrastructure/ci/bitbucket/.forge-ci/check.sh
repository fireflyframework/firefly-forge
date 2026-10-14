#!/usr/bin/env bash
# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
set -euo pipefail
export PATH="$PWD/.forge-ci/bin:$PATH"
mkdir -p security
# A real product gate must exist; missing toolchains or checks fail the job.
make check
gitleaks git --redact --exit-code 1 --report-format json --report-path security/gitleaks.json .
trivy fs --scanners vuln,misconfig,secret --severity HIGH,CRITICAL --exit-code 1 \
  --format json --output security/filesystem-report.json .
trivy fs --scanners vuln --format cyclonedx --output security/source-sbom.json .
image="ghcr.io/example/inventory:${CI_REVISION:?Set the immutable source commit revision}"
docker build --pull --tag "$image" .
docker save "$image" --output security/image.tar
trivy image --input security/image.tar --severity HIGH,CRITICAL --exit-code 1 \
  --format json --output security/image-report.json
trivy image --input security/image.tar --format cyclonedx --output security/image-sbom.json
if [ "${CI_PUBLISH:-false}" = true ]; then
  : "${REGISTRY_USERNAME:?Configure registry username in the CI platform}"
  : "${REGISTRY_PASSWORD:?Configure registry credentials in the CI platform}"
  printf "%s" "$REGISTRY_PASSWORD" | docker login "ghcr.io" --username "$REGISTRY_USERNAME" --password-stdin
  docker push "$image"
  repository="ghcr.io/example/inventory"
  reference=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image" |
    awk -v repository="$repository" 'index($0, repository "@sha256:") == 1 {print; exit}')
  digest=${reference##*@sha256:}
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || { echo 'Registry did not return the expected image digest'; exit 1; }
  printf '%s\n' "$reference" > security/image-reference.txt
fi
