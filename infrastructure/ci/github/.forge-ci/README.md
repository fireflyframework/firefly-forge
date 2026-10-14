# Reviewed product CI

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

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
