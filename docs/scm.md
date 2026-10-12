<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Connect and publish Git repositories

Forge works with a Git clone URL or an absolute local repository path. The
server's Git installation and credentials determine clone access. Register the
repository's real default branch; Forge does not guess it.

## Register a source

Use **Repositories → Add repository** or `POST /api/v1/repositories`. The provider
values are `git`, `github`, `gitlab`, `bitbucket`, `azure` and `gitea`. Generic Git
supports local generation without a provider-specific pull-request API.

HTTPS URLs must not embed usernames or passwords. Configure a Git credential
helper, SSH agent or other appropriate server-side authentication. A successful
clone proves repository access; it does not prove pull-request permissions.

## Publish a verified change

Studio starts local runs with `publish: false`. After a successful run, **Publish
pull request** requests remote publication. CLI operators can use `forge publish
RUN_ID`. API operators can also explicitly
request `publish: true`. That request authorizes the engine's remote publication
step after its verification gates. The step commits the generated change,
pushes the run branch and invokes the selected pull-request adapter.

| Provider | Pull-request adapter |
| --- | --- |
| GitHub | GitHub repository pull request API |
| GitLab | GitLab merge request API |
| Bitbucket | Bitbucket Cloud pull request API |
| Azure DevOps | Azure Git pull request API |
| Gitea | Gitea pull request API |
| Generic Git | No provider-specific pull request operation |

These are implemented adapter paths, not a claim that every host, account or
enterprise installation has been exercised live. Configure the server's host
and token settings, verify account permissions and inspect actual remote
responses. A test of request construction does not prove a created pull request.

Publication is not deployment. A pull request does not merge or release a
product. The receiving project's reviews, CI and release rules remain in force.

Provider publication tokens use `FORGE_GITHUB_TOKEN`, `FORGE_GITLAB_TOKEN`,
`FORGE_BITBUCKET_TOKEN`, `FORGE_AZURE_TOKEN` or `FORGE_GITEA_TOKEN`. Enterprise API
bases can be set with `FORGE_GITHUB_API`, `FORGE_GITLAB_API` and `FORGE_GITEA_API`.
The token used for the pull-request API is separate from Git push authentication.
