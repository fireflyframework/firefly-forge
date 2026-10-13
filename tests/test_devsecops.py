# Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0
import subprocess

import pytest
import yaml

from firefly_forge.devsecops import render_ci


@pytest.mark.parametrize("provider", ["github", "gitlab", "bitbucket"])
@pytest.mark.parametrize("backend", ["pyfly", "larafly"])
def test_ci_runs_real_project_and_security_gates_before_image_publication(tmp_path, provider, backend):
    files = render_ci(provider, backend, "react", "ghcr.io/example/inventory")
    pipeline = next(content for name, content in files.items() if name.endswith(".yml"))
    assert isinstance(yaml.safe_load(pipeline), dict)
    check = files[".forge-ci/check.sh"]
    assert "make check" in check and "gitleaks git" in check
    assert "--exit-code 1" in check and "HIGH,CRITICAL" in check
    assert "cyclonedx" in check and "docker build" in check and "docker push" in check
    assert check.index("trivy image") < check.index("docker push")
    assert "image-reference.txt" in check and "sha256:" in check
    assert "sha256sum -c" in files[".forge-ci/bootstrap.sh"]
    for name, content in files.items():
        if name.endswith(".sh"):
            path = tmp_path / name.rsplit("/", 1)[-1]
            path.write_text(content)
            assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0


def test_oidc_signing_is_scoped_to_supported_protected_jobs():
    github = yaml.safe_load(
        render_ci("github", "pyfly", "none", "ghcr.io/org/app", signing=True)[".github/workflows/forge-ci.yml"]
    )
    assert "id-token" not in github["jobs"]["check"].get("permissions", {})
    assert github["jobs"]["release"]["permissions"]["id-token"] == "write"
    assert "refs/heads/main" in github["jobs"]["release"]["if"]
    gitlab = yaml.safe_load(
        render_ci("gitlab", "larafly", "none", "registry.gitlab.com/org/app", signing=True)[".gitlab-ci.yml"]
    )
    assert gitlab["release"]["id_tokens"]["SIGSTORE_ID_TOKEN"]["aud"] == "sigstore"
    assert "id_tokens" not in gitlab["check"]
    with pytest.raises(ValueError, match="Bitbucket"):
        render_ci("bitbucket", "pyfly", "none", "registry.example.com/org/app", signing=True)


@pytest.mark.parametrize(
    "values",
    [
        ("unknown", "pyfly", "react", "ghcr.io/org/app"),
        ("github", "java", "react", "ghcr.io/org/app"),
        ("github", "pyfly", "react", "ghcr.io/org/app;curl bad"),
        ("github", "pyfly", "react", "https://user:password@registry.example/app"),
    ],
)
def test_ci_rejects_unknown_profiles_and_shell_or_credential_registry_inputs(values):
    with pytest.raises(ValueError):
        render_ci(*values)


def test_vulnerable_image_stops_real_shell_flow_before_any_registry_push(tmp_path):
    import os

    files = render_ci("github", "pyfly", "none", "ghcr.io/example/inventory")
    directory = tmp_path / ".forge-ci"
    binary = directory / "bin"
    binary.mkdir(parents=True)
    script = directory / "check.sh"
    script.write_text(files[".forge-ci/check.sh"])
    trace = tmp_path / "trace"
    for name in ["make", "gitleaks", "trivy", "docker"]:
        tool = binary / name
        tool.write_text(f'''#!/usr/bin/env bash
printf '%s\\n' '{name}' >> "{trace}"
if [ '{name}' = trivy ] && [ "$1" = image ]; then exit 7; fi
if [ '{name}' = docker ] && [ "$1" = push ]; then printf push >> "{trace}"; fi
exit 0
''')
        tool.chmod(0o700)
    result = subprocess.run(
        ["bash", str(script)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CI_REVISION": "a" * 40,
            "CI_PUBLISH": "true",
            "REGISTRY_USERNAME": "fixture",
            "REGISTRY_PASSWORD": "fixture",
        },
    )
    assert result.returncode == 7
    assert "push" not in trace.read_text()
    assert trace.read_text().splitlines()[:2] == ["make", "gitleaks"]
