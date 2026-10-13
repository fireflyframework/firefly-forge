---
description: Review infrastructure recipes for AWS, Azure, Google Cloud, DigitalOcean and private servers, with explicit local qualification limits.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Review and deploy a cloud recipe

Cloud recipes turn an independently verified product into a reviewable VM and application target. The catalog includes AWS EC2, Azure Linux VM, Google Compute Engine, DigitalOcean Droplet, an existing VPS and a dedicated on-premises host. The recipes provision VMs. Managed Kubernetes and managed database installation are outside their scope.

Rendering returns files, a target preview, requirements and a content digest. It neither authenticates a cloud account nor applies infrastructure. Install OpenTofu 1.11+ (or review a compatible Terraform configuration), Ansible Core 2.16+, Python 3 and curl on the deployment host. The generated commands use `tofu` and `ansible-playbook` from its `PATH`. Save or export the recipe to review it. Enabling a deployment target remains separate from explicitly deploying a verified run.

## Use the recipe API

The authenticated prefix is `/api/v1`. Read `GET /cloud/providers`, then submit `CloudRecipeInput` to `POST /cloud/recipes/render` for a preview. `POST /cloud/recipes` saves a reviewed recipe privately; `GET /cloud/recipes/{id}/archive.zip` exports its bounded source files. `POST /cloud/recipes/{id}/configure-target` takes `{"target_id":"production"}` and requires its explicit credential profile. None of these routes deploys. The existing run deployment action is the separate infrastructure authorization.

## Prepare the product image

Build and scan the product's root Dockerfile in a trusted CI pipeline. Retain the source revision, image scan, SBOM and pushed image digest. Use an image reference such as `registry.example.com/team/inventory@sha256:...`, with all 64 digest characters. Mutable tags are rejected.

The deployment gate checks the run's verified source tree. That gate alone does not prove that an independently supplied registry digest was built from that tree. Match the CI provenance and source revision before choosing the image; verify a signing identity and issuer when signing is enabled. [DevSecOps](devsecops.md) explains the generated CI gates.

## Choose the provider inputs

All recipes need the repository ID, a lowercase deployment name, image digest, domain, health path, container port, SSH user/public key and restricted IPv4 administration CIDR. A previous image digest enables application rollback. Rendering can omit credentials, but enabling a target requires an explicit private deployment profile. Credentials never belong in recipe text.

| Provider | Required configuration fields | Optional fields |
| --- | --- | --- |
| AWS | `region`, `vpc_id`, `subnet_id`, `image_id`, `state_bucket` | `size`, `state_key` |
| Azure | `region`, `subscription_id`, `state_resource_group`, `state_storage_account`, `state_container` | `size`, `state_key` |
| GCP | `project`, `region`, `zone`, `state_bucket` | `size`, `image_id`, `state_key` |
| DigitalOcean | `region`, `ssh_key_id`, `state_bucket`, `state_region`, `state_endpoint` | `size`, `image_id`, `state_key` |
| VPS / on-premises | `host` | — |

AWS requires an explicit Ubuntu AMI suitable for its region and instance architecture. Azure, GCP and DigitalOcean examples use Ubuntu 24.04. Confirm regional image and size availability before enabling a target. DigitalOcean's SSH key ID must already exist in that account.

Cloud state lives in an existing backend: S3 for AWS, Azure Blob for Azure, GCS for GCP, and Spaces with an S3-compatible backend for DigitalOcean. Use a unique state key per project and target. Do not share one key between independent installations. Protect state as sensitive operational data, enable access controls/versioning, and verify its locking behavior.

Provider versions are constrained to the validated patch series: AWS `~> 6.68.0`, AzureRM `~> 5.9.0`, Google `~> 8.6.0` and DigitalOcean `~> 2.106.0`. Initialization selects a provider within that series and writes a lock file. Review provider updates and remote-state configuration before applying a changed recipe.

## Configure deployment access separately

Select a dedicated deployment credential profile. AWS uses its access key, secret and optional session token; Azure uses its client/tenant/subscription configuration; GCP can use a selected service-account credential file; DigitalOcean needs a Droplet API token and separate Spaces backend credentials. Prefer short-lived identities and the narrowest account/resource permissions that meet the target's needs.

The factory freezes selected profile credentials privately for the deployment. Coding workers and product checks do not receive them. Credentials and private key contents are not recipe inputs. SSH authentication uses the selected agent socket or private-key file and an independently verified known-hosts file selected in that profile; host-key verification is never disabled. Prepare registry authentication on the VM separately when using a private image.

Deployment commands use an isolated home/cache directory. They do not discover credentials from the server’s ordinary home directory, SSH keys or cloud metadata fallback. Select the required credential/config files explicitly in the profile. Ambient workload-identity authentication is not enabled by these recipes. Confirm actual provider access before deployment.

## Prepare the remote host

Use a dedicated Ubuntu 24.04 host with sudo SSH access. Independently manage:

- DNS pointing the domain at that host.
- A trusted TLS certificate and private key under `/etc/letsencrypt/live/DOMAIN`.
- The application environment file `/etc/forge/NAME.env`, owned by root with mode `0600`.
- Registry authentication and external database/storage services.
- Verified SSH host keys and, for existing hosts, firewall rules permitting restricted administration plus HTTP/HTTPS.

The playbook refuses absent secrets or certificates. It installs distro Docker/nginx packages, pulls the digest, rejects a root-user image, and runs a confined container with dropped capabilities, a read-only root filesystem, bounded memory/processes and writable `/tmp`. Application traffic binds to loopback; nginx terminates TLS and redirects HTTP to HTTPS. Configure application proxy trust correctly for its framework.

These defaults suit a stateless service. They do not provision a product database, durable upload volume, backups, certificate renewal service or application-specific migrations. Configure those separately and test them. Replacing the named container can briefly interrupt service; this recipe does not implement a zero-downtime rollout.

A newly provisioned VM will not yet have the external application secrets and TLS certificate. The playbook stops at those prerequisites. Complete them through the approved host-management process, verify its SSH identity, then explicitly deploy again against the retained remote state. It does not create secrets or bypass certificate verification.

## Inspect the actual effects

Cloud targets execute OpenTofu initialization, validation, plan and application, then retrieve the host address and run Ansible. Existing-host targets run Ansible directly. A final HTTPS check uses the configured health path and validates the certificate. The job records each actual command's write-ahead intent, exit result and completion receipt.

The deployment request authorizes infrastructure effects, including billable resources. Review the generated files and target commands beforehand. If your organization requires a separately approved cloud plan, use its normal plan review pipeline before enabling the apply target; the factory's preview is source configuration, not an authenticated provider plan.

Rollback reinitializes the same remote state to find the host and runs the retained application playbook with the previous image digest. It does not destroy the VM or reverse database migrations, external side effects or infrastructure changes. Inspect uncertain outcomes before another operation. [Deployment](deployment.md) and [recovery](recovery.md) explain evidence and reconciliation.

## Validation boundary

Local acceptance checks the rendered Terraform against actual provider schemas with remote backends disabled, checks Ansible syntax and tests private exports and target materialization. No cloud account, VM or remote application was created by those checks. A live deployment succeeds only when its own provider calls, SSH changes and HTTPS verification return durable receipts.

The resource and backend contracts come from the [AWS provider](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/instance), [AzureRM provider](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/linux_virtual_machine), [Google provider](https://registry.terraform.io/providers/hashicorp/google/latest/docs/resources/compute_instance), [DigitalOcean provider](https://registry.terraform.io/providers/digitalocean/digitalocean/latest/docs/resources/droplet), [DigitalOcean remote-state guidance](https://docs.digitalocean.com/products/spaces/reference/terraform-backend/) and [OpenTofu JSON configuration](https://opentofu.org/docs/language/syntax/json/).
