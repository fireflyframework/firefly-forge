# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
"""Reviewable CI source files with executable gates, never automatic repository writes."""

from __future__ import annotations

import re
from typing import Any

import yaml  # type: ignore[import-untyped]

CHECKOUT = "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803"
UPLOAD = "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
UBUNTU = "ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55"
DIND = "docker:29-dind@sha256:0b6d18a4a222e71c88be6034926b4f6cde2fc33c2218643d03ccfddc65995470"
TOOLS = [
    (
        "gitleaks",
        "https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
    (
        "trivy",
        "https://github.com/aquasecurity/trivy/releases/download/v0.75.0/trivy_0.75.0_Linux-64bit.tar.gz",
        "c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f",
    ),
    (
        "cosign",
        "https://github.com/sigstore/cosign/releases/download/v3.1.3/cosign-linux-amd64",
        "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71",
    ),
    (
        "uv",
        "https://github.com/astral-sh/uv/releases/download/0.13.0/uv-x86_64-unknown-linux-gnu.tar.gz",
        "1468ebd5a5541121837c5a2817b9972ba6090fa6caa3d142620850a47fb75154",
    ),
    (
        "node",
        "https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz",
        "df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de",
    ),
]


def ci_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": provider,
            "backends": ["pyfly", "larafly"],
            "frontends": ["none", "react", "angular", "nextjs", "vue"],
            "signing": provider in {"github", "gitlab"},
            "state": "template_available",
            "remote_pipeline_verified": False,
        }
        for provider in ["github", "gitlab", "bitbucket"]
    ]


def _bootstrap(backend: str, frontend: str, signing: bool) -> str:
    result = """#!/usr/bin/env bash
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
"""
    for name, url, digest in TOOLS:
        if (
            name == "cosign"
            and not signing
            or name == "uv"
            and backend != "pyfly"
            or name == "node"
            and frontend == "none"
        ):
            continue
        filename = url.rsplit("/", 1)[-1]
        result += f'curl --fail --silent --show-error --location "{url}" -o ".forge-ci/downloads/{filename}"\n'
        result += f'printf "%s  %s\\n" "{digest}" ".forge-ci/downloads/{filename}" | sha256sum -c -\n'
        if name == "cosign":
            result += f'install -m 0755 ".forge-ci/downloads/{filename}" .forge-ci/bin/cosign\n'
        elif name == "node":
            result += f'tar -xJf ".forge-ci/downloads/{filename}" -C .forge-ci --strip-components=1\n'
        elif name == "uv":
            result += f'tar -xzf ".forge-ci/downloads/{filename}" -C .forge-ci/bin --strip-components=1\n'
        else:
            result += f'tar -xzf ".forge-ci/downloads/{filename}" -C .forge-ci/bin {name}\n'
    if backend == "pyfly":
        result += (
            "uv python install 3.13\n"
            'ln -sf "$(uv python find 3.13)" .forge-ci/bin/python3\n'
            'ln -sf "$(uv python find 3.13)" .forge-ci/bin/python\n'
        )
    else:
        result += (
            '"${sudo_cmd[@]}" apt-get install -y php8.3-cli php8.3-xml php8.3-mbstring '
            "php8.3-curl php8.3-sqlite3 php8.3-zip composer\n"
        )
    return result


def _check(repository: str, signing: bool) -> str:
    sign = 'cosign sign --yes "$reference"\n' if signing else ""
    return (
        """#!/usr/bin/env bash
set -euo pipefail
export PATH="$PWD/.forge-ci/bin:$PATH"
mkdir -p security
# A real product gate must exist; missing toolchains or checks fail the job.
make check
gitleaks git --redact --exit-code 1 --report-format json --report-path security/gitleaks.json .
trivy fs --scanners vuln,misconfig,secret --severity HIGH,CRITICAL --exit-code 1 \\
  --format json --output security/filesystem-report.json .
trivy fs --scanners vuln --format cyclonedx --output security/source-sbom.json .
"""
        + f'image="{repository}:${{CI_REVISION:?Set the immutable source commit revision}}"\n'
        + """docker build --pull --tag "$image" .
docker save "$image" --output security/image.tar
trivy image --input security/image.tar --severity HIGH,CRITICAL --exit-code 1 \\
  --format json --output security/image-report.json
trivy image --input security/image.tar --format cyclonedx --output security/image-sbom.json
if [ "${CI_PUBLISH:-false}" = true ]; then
  : "${REGISTRY_USERNAME:?Configure registry username in the CI platform}"
  : "${REGISTRY_PASSWORD:?Configure registry credentials in the CI platform}"
"""
        + f'  printf "%s" "$REGISTRY_PASSWORD" | docker login "{repository.split("/", 1)[0]}" '
        '--username "$REGISTRY_USERNAME" --password-stdin\n'
        + """  docker push "$image"
"""
        + f'  repository="{repository}"\n'
        + """  reference=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image" |
    awk -v repository="$repository" 'index($0, repository "@sha256:") == 1 {print; exit}')
  digest=${reference##*@sha256:}
  [[ "$digest" =~ ^[a-f0-9]{64}$ ]] || { echo 'Registry did not return the expected image digest'; exit 1; }
  printf '%s\\n' "$reference" > security/image-reference.txt
"""
        + sign
        + "fi\n"
    )


def render_ci(
    provider: str, backend: str, frontend: str, image_registry: str, *, signing: bool = False
) -> dict[str, str]:
    if provider not in {"github", "gitlab", "bitbucket"} or backend not in {"pyfly", "larafly"}:
        raise ValueError("Choose GitHub, GitLab or Bitbucket with PyFly or LaraFly")
    if frontend not in {"none", "react", "angular", "nextjs", "vue"}:
        raise ValueError("Unsupported frontend CI profile")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*(?::[0-9]{1,5})?/[a-z0-9][a-z0-9._/-]{0,180}", image_registry):
        raise ValueError("Use a credential-free registry/repository without a tag or shell characters")
    if signing and provider == "bitbucket":
        raise ValueError("Bitbucket keyless signing is not provided; configure and verify a separate signing flow")
    files = {
        ".forge-ci/bootstrap.sh": _bootstrap(backend, frontend, signing),
        ".forge-ci/check.sh": _check(image_registry, signing),
    }
    pipeline: dict[str, Any]
    if provider == "github":
        checkout = {"uses": CHECKOUT, "with": {"fetch-depth": 0, "persist-credentials": False}}
        upload = {
            "name": "Retain security reports and SBOMs",
            "if": "always()",
            "uses": UPLOAD,
            "with": {
                "name": "security-${{ github.job }}",
                "path": "security/*.json\nsecurity/image-reference.txt",
                "retention-days": 30,
            },
        }
        steps = [
            checkout,
            {"name": "Install checksum-pinned tooling", "run": "bash .forge-ci/bootstrap.sh"},
            {"name": "Run product and security gates", "run": "bash .forge-ci/check.sh"},
            upload,
        ]
        credentials = {
            "REGISTRY_USERNAME": "${{ secrets.REGISTRY_USERNAME }}",
            "REGISTRY_PASSWORD": "${{ secrets.REGISTRY_PASSWORD }}",
        }
        permissions = {"contents": "read"}
        if image_registry.startswith("ghcr.io/"):
            credentials = {"REGISTRY_USERNAME": "${{ github.actor }}", "REGISTRY_PASSWORD": "${{ github.token }}"}
            permissions["packages"] = "write"
        if signing:
            permissions["id-token"] = "write"
        pipeline = {
            "name": "Verified product and image",
            "on": {"pull_request": {}, "push": {"branches": ["main"]}},
            "permissions": {"contents": "read"},
            "jobs": {
                "check": {
                    "runs-on": "ubuntu-24.04",
                    "env": {"CI_REVISION": "${{ github.sha }}", "CI_PUBLISH": "false"},
                    "steps": steps,
                },
                "release": {
                    "needs": "check",
                    "if": "github.event_name == 'push' && github.ref == 'refs/heads/main'",
                    "runs-on": "ubuntu-24.04",
                    "permissions": permissions,
                    "env": {"CI_REVISION": "${{ github.sha }}", "CI_PUBLISH": "true", **credentials},
                    "steps": steps,
                },
            },
        }
        filename = ".github/workflows/forge-ci.yml"
    elif provider == "gitlab":
        base = {
            "image": UBUNTU,
            "services": [{"name": DIND, "alias": "docker"}],
            "variables": {
                "DOCKER_HOST": "tcp://docker:2375",
                "DOCKER_TLS_CERTDIR": "",
                "GIT_DEPTH": "0",
                "CI_REVISION": "$CI_COMMIT_SHA",
            },
            "before_script": ["bash .forge-ci/bootstrap.sh"],
            "script": ["bash .forge-ci/check.sh"],
            "artifacts": {
                "when": "always",
                "paths": ["security/*.json", "security/image-reference.txt"],
                "expire_in": "30 days",
            },
        }
        release: dict[str, Any] = {
            "extends": ".forge",
            "stage": "release",
            "variables": {
                "CI_PUBLISH": "true",
                "REGISTRY_USERNAME": "$REGISTRY_USERNAME",
                "REGISTRY_PASSWORD": "$REGISTRY_PASSWORD",
            },
            "rules": [{"if": '$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH && $CI_PIPELINE_SOURCE == "push"'}],
        }
        if image_registry.startswith("registry.gitlab.com/"):
            release["variables"].update(
                REGISTRY_USERNAME="$CI_REGISTRY_USER", REGISTRY_PASSWORD="$CI_REGISTRY_PASSWORD"
            )
        if signing:
            release["id_tokens"] = {"SIGSTORE_ID_TOKEN": {"aud": "sigstore"}}
            release["script"] = ['test "$CI_SERVER_URL" = https://gitlab.com', "bash .forge-ci/check.sh"]
        pipeline = {
            "stages": ["check", "release"],
            ".forge": base,
            "check": {"extends": ".forge", "stage": "check", "variables": {"CI_PUBLISH": "false"}},
            "release": release,
        }
        filename = ".gitlab-ci.yml"
    else:

        def step(publish: bool) -> dict[str, Any]:
            return {
                "step": {
                    "name": "Verify and publish" if publish else "Verify product and image",
                    "services": ["docker"],
                    "script": [
                        f'export CI_REVISION="$BITBUCKET_COMMIT" CI_PUBLISH={"true" if publish else "false"}',
                        "bash .forge-ci/bootstrap.sh",
                        "bash .forge-ci/check.sh",
                    ],
                    "artifacts": ["security/*.json", "security/image-reference.txt"],
                }
            }

        pipeline = {
            "image": UBUNTU,
            "clone": {"depth": "full"},
            "pipelines": {"pull-requests": {"**": [step(False)]}, "branches": {"main": [step(True)]}},
        }
        filename = "bitbucket-pipelines.yml"
    files[filename] = (
        "# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0\n"
        + yaml.safe_dump(pipeline, sort_keys=False, width=100)
    )
    files[".forge-ci/README.md"] = """# Reviewed product CI

These templates execute on trusted Linux x86_64 CI runners. Install through a reviewed branch and configure
your provider's protected main branch, runner and registry permissions. `make check` and a root Dockerfile
must be real project gates; missing files or tools fail the pipeline.

Scans fail on detected secrets and high/critical findings. Reports and CycloneDX SBOMs are retained as CI
artifacts; they are evidence, not a compliance certificate. Registry publication only runs for the configured
protected main/default branch. The returned image-reference artifact contains the pushed immutable digest for
deployment.

Private registry credentials remain in the CI platform. GitHub GHCR and GitLab.com registry use native job
credentials. Other registries require secured REGISTRY_USERNAME/REGISTRY_PASSWORD values. Never expose them to
pull-request pipelines. Use separate trusted runners and provider branch restrictions; checked-out project
code executes on the runner.

Optional Cosign OIDC is supported for GitHub and GitLab.com; self-managed GitLab needs its own trusted
Sigstore setup and is rejected by the public signing job. Bitbucket signing is not generated. Verify the exact
certificate identity and OIDC issuer before accepting a signed digest; signing alone does not prove the
product meets its requirements.
"""
    return files
