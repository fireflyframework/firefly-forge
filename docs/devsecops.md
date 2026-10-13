<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Install executable DevSecOps checks

The CI renderer produces reviewable source files for GitHub Actions, GitLab CI and Bitbucket Pipelines. PyFly and LaraFly profiles can include the supported browser frontends. The files contain checks and publication rules. Generating or exporting them does not install a workflow or establish that a remote pipeline has passed.

Copy the files into a reviewed branch of the product repository. Protect the default/main branch, restrict runner access, and configure registry permissions in the CI platform. Keep its real `make check` gate and root Dockerfile. Missing checks or tooling fail the pipeline.

## Render the CI files

Read authenticated `GET /api/v1/devsecops/providers`. Submit `POST /api/v1/devsecops/templates`:

```json
{
  "provider": "github",
  "backend": "pyfly",
  "frontend": "react",
  "image_registry": "ghcr.io/your-org/inventory",
  "signing": true
}
```

The response returns relative source paths and file contents, with `remote_pipeline_verified: false`. Review and install them through the product repository’s normal source-control process. The endpoint does not write a branch, create provider secrets or run a remote pipeline.

## Follow the enforced sequence

| Step | Actual operation | Failure behavior |
| --- | --- | --- |
| Product | `make check` | A failing or missing product gate stops the job |
| Secret detection | Gitleaks scans Git history with redacted findings | Detected secrets stop the job |
| Source security | Trivy scans vulnerabilities, misconfiguration and secrets | High/critical findings stop the job |
| Source inventory | Trivy emits a CycloneDX SBOM | A scanner error stops the job |
| Container | Docker builds the root Dockerfile and saves its image | Build errors stop the job |
| Image security | Trivy scans the saved image and emits its SBOM | High/critical findings stop publication |
| Publication | The authorized release job pushes the image and records its digest | Registry errors stop release |
| Optional signing | Cosign signs the pushed digest using supported CI OIDC | Signing failure fails the release job |

Failed GitHub/GitLab jobs still upload any available JSON reports. Successful jobs retain source/image SBOMs and `security/image-reference.txt`. Bitbucket artifacts follow its platform's completed-step behavior; inspect the live pipeline when a step fails. Findings and SBOMs are evidence, not a compliance certificate or proof that every vulnerability is detectable.

Security exceptions require review. Change a project policy or scanner configuration explicitly and retain the rationale. Do not replace a failing command with an unconditional success or disable the platform's mandatory source/verification guards. A leaked secret requires revocation and investigation even after a later scan passes.

## Use pinned tooling

GitHub actions use full commit SHAs. GitLab/Bitbucket runner images and GitLab's Docker service use OCI digests. Bootstrap scripts verify fixed SHA-256 checksums before installing their downloaded tools. Current templates use Gitleaks 8.30.1, Trivy 0.75.0, Cosign 3.1.3, uv 0.13.0 and Node.js 22.23.3, with Python 3.13 or PHP 8.3 for the selected backend.

The templates target trusted Linux x86_64 runners. GitLab requires a runner configured for its Docker-in-Docker service; its isolated job network exposes the daemon internally. Bitbucket uses its provided Docker service. Container build access is powerful: isolate these runners from production credentials and unrelated infrastructure.

Pinned tool versions do not freeze the vulnerability database or OS package feeds. Scanner databases need network access and change as advisories arrive. Review tool pins and baseline updates through normal source review, then rerun the pipeline.

## Separate review checks from release authority

Pull-request/check jobs do not receive generated registry publication or signing permissions. The GitHub release job runs only for a push to `main`; GitLab uses its default branch; Bitbucket's release step uses `main`. Adjust the branch selection only through a reviewed change, and enable the corresponding provider protection rules.

GitHub GHCR publication uses its native job token with package-write permission. GitLab.com registry publication uses native job registry credentials. Other registries use secured `REGISTRY_USERNAME` and `REGISTRY_PASSWORD` values managed by the CI platform. Keep those values protected and unavailable to untrusted pull-request work. Shell scripts do not enable command tracing, and Docker login receives its password through standard input.

An arbitrary product test or Dockerfile executes code on the runner. Branch conditions alone do not make a persistent self-hosted runner safe for untrusted contributions. Use ephemeral isolated review runners and separate protected release runners where appropriate.

## Opt into supported signing

GitHub signing requests `id-token: write` only for its protected release job. GitLab.com signing requests a `SIGSTORE_ID_TOKEN` with audience `sigstore` in the same job that builds, scans and pushes the image. A generated GitLab signing job rejects a self-managed server; that server needs separately configured trusted Sigstore infrastructure. No Bitbucket signing flow is generated.

Signing covers the pushed immutable image reference. Verify its exact certificate identity and OIDC issuer before deploying it. For GitLab.com, the identity includes the documented double slash before the CI filename. Preserve the source commit, image digest, SBOM and registry signature as related evidence. A signature identifies the signing workflow; it does not replace product or security verification.

The flows follow [GitHub workflow permissions](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax), [GitLab keyless signing](https://docs.gitlab.com/ci/yaml/signing_examples/), [Sigstore container signing](https://docs.sigstore.dev/cosign/signing/signing_with_containers/) and [Bitbucket Docker execution](https://support.atlassian.com/bitbucket-cloud/docs/run-docker-commands-in-bitbucket-pipelines/). Scanner behavior comes from [Gitleaks](https://github.com/gitleaks/gitleaks) and [Trivy](https://github.com/aquasecurity/trivy).

## Accept the pipeline and deploy the digest

Inspect the installed provider pipeline on a real product: a failing test must stop the gate, an intentionally introduced test secret must fail safely without exposing its value, and a vulnerable image fixture must block publication. Remove those fixtures through source review after acceptance. Then confirm the actual registry digest, reports and optional signing identity.

The factory's local renderer checks YAML/shell structure and failure-flow behavior. It does not claim live GitHub, GitLab or Bitbucket execution until that provider has run the installed files. Use the resulting digest and matching source provenance in a [cloud deployment recipe](cloud-deployment.md); do not infer image provenance from a passing source run alone.
