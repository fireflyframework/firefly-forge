<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Reviewed cloud and CI recipes

The canonical renderers are `firefly_forge.cloud` and `firefly_forge.devsecops`. They run from an installed wheel; these example files are review aids, not runtime dependencies. Rendering and exporting do not contact a cloud account or apply infrastructure.

`examples/` shows AWS EC2, Azure Linux VM, Google Compute Engine, DigitalOcean Droplet and existing dedicated VPS/on-premises host recipes. Cloud configurations use remote state and restricted SSH ingress. Container images use a digest, and application rollback requires a previous reviewed digest.

Replace all illustrative values. The example image digest is a placeholder and the public SSH key is a test fixture: neither is suitable for deployment. Render a new recipe with the actual reviewed image, operator public key, repository and cloud identifiers.

Before enabling a target, prepare a dedicated Ubuntu 24.04 host identity, private remote state with locking, an explicit provider-scoped deployment credential profile, selected SSH authentication and known-hosts files and domain DNS. Independently install the remote application's root-owned `0600` environment file and managed TLS certificate. Private registry authentication also belongs on the target. The playbook refuses missing secrets/certificates and requires a nonroot image with a read-only root filesystem. Its application port is bound to loopback behind an HTTPS nginx proxy.

`ci/` contains source-control platform examples. Copy them through a reviewed branch, configure protected branches and trusted runners, and supply registry permissions through the CI platform. The scripts execute real product, secret and vulnerability checks; generated templates alone do not mean those checks have passed. GitHub and GitLab.com can opt into OIDC signing. No Bitbucket signing flow is implied.

No cloud account, VM, registry publication or provider CI pipeline was created to validate these examples. Local validation covers Terraform provider schemas, Ansible syntax, shell/YAML syntax and failure-gate behavior. Consult the cloud deployment and DevSecOps guides for prerequisites, evidence and operational limits.
