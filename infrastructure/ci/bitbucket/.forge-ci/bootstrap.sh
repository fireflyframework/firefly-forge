#!/usr/bin/env bash
set -euo pipefail
[ "$(uname -m)" = x86_64 ] || { echo 'Use a trusted Linux x86_64 CI runner'; exit 1; }
sudo_cmd=()
if [ "$(id -u)" != 0 ]; then sudo_cmd=(sudo); fi
export DEBIAN_FRONTEND=noninteractive
"${sudo_cmd[@]}" apt-get update
"${sudo_cmd[@]}" apt-get install -y ca-certificates curl git make xz-utils
if ! command -v docker >/dev/null; then "${sudo_cmd[@]}" apt-get install -y docker.io; fi
mkdir -p .forge-ci/bin .forge-ci/downloads security
export PATH="$PWD/.forge-ci/bin:$PATH"
curl --fail --silent --show-error --location "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz" -o ".forge-ci/downloads/gitleaks_8.30.1_linux_x64.tar.gz"
printf "%s  %s\n" "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb" ".forge-ci/downloads/gitleaks_8.30.1_linux_x64.tar.gz" | sha256sum -c -
tar -xzf ".forge-ci/downloads/gitleaks_8.30.1_linux_x64.tar.gz" -C .forge-ci/bin gitleaks
curl --fail --silent --show-error --location "https://github.com/aquasecurity/trivy/releases/download/v0.75.0/trivy_0.75.0_Linux-64bit.tar.gz" -o ".forge-ci/downloads/trivy_0.75.0_Linux-64bit.tar.gz"
printf "%s  %s\n" "c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f" ".forge-ci/downloads/trivy_0.75.0_Linux-64bit.tar.gz" | sha256sum -c -
tar -xzf ".forge-ci/downloads/trivy_0.75.0_Linux-64bit.tar.gz" -C .forge-ci/bin trivy
curl --fail --silent --show-error --location "https://github.com/astral-sh/uv/releases/download/0.13.0/uv-x86_64-unknown-linux-gnu.tar.gz" -o ".forge-ci/downloads/uv-x86_64-unknown-linux-gnu.tar.gz"
printf "%s  %s\n" "1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154" ".forge-ci/downloads/uv-x86_64-unknown-linux-gnu.tar.gz" | sha256sum -c -
tar -xzf ".forge-ci/downloads/uv-x86_64-unknown-linux-gnu.tar.gz" -C .forge-ci/bin --strip-components=1
curl --fail --silent --show-error --location "https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz" -o ".forge-ci/downloads/node-v22.23.3-linux-x64.tar.xz"
printf "%s  %s\n" "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de" ".forge-ci/downloads/node-v22.23.3-linux-x64.tar.xz" | sha256sum -c -
tar -xJf ".forge-ci/downloads/node-v22.23.3-linux-x64.tar.xz" -C .forge-ci --strip-components=1
uv python install 3.13
ln -sf "$(uv python find 3.13)" .forge-ci/bin/python3
ln -sf "$(uv python find 3.13)" .forge-ci/bin/python
