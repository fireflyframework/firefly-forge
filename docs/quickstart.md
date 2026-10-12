<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Build your first product

This walkthrough produces a local change and verification evidence. It does not
push a branch or create a pull request. Complete [installation](installation.md)
and connect Studio to the API first.

## 1. Register a repository

In **Repositories**, select **Add repository**. Enter its name, Git clone URL,
provider and actual default branch. For a local exercise, use an absolute path
to a repository with at least one committed file. Use provider **Generic Git**.

forge's server must be able to clone the repository. Existing uncommitted work
in your source checkout is not copied into the run. Configure authentication
through the server's Git credential helper or SSH agent; never embed credentials
in the URL.

## 2. Write the brief

Open **New product** and select the repository. Use a specific, bounded brief:

```text
Build a PyFly API for a small task list. Users can create a task, list tasks,
mark one complete and receive a 404 for an unknown task. Use typed request and
response contracts, a service for task behavior and meaningful pytest tests.
Keep this first version in memory. Include exact launch instructions, declared
dependencies, lint/type checks and a make check target. Do not add a frontend.
```

Choose **PyFly**, **API only**, and a ready coding provider. For a PHP product,
use the [LaraFly walkthrough](larafly.md).

## 3. Choose verification

If your brief requires a `make check` target, enter:

```json
[["make", "check"]]
```

Each Studio command has a 120-second timeout. The [HTTP API](api.md) allows an
explicit timeout up to the server's accepted limit. For a generated project
whose command is not known in advance, leave this field empty and let Forge
select a supported verification command from the actual project files.

## 4. Follow the run

Select **Start building**. Studio opens the run detail. Its timeline records
progress, while **Changes** shows the changed paths and diff. **Verification**
contains the commands and their real results. **Artifacts** lists retained
outputs reported by the server.

Expected: a successful run contains a generated change and passing verification.
A failed command or provider error leaves a failed run with evidence; it must
not be treated as a passing build. If a run is still active, **Cancel run**
requests cancellation. **Retry run** creates another attempt; inspect both.

## 5. Review the evidence

Read the diff, verification output and generated README. Run the product's
startup command, exercise the documented behavior and inspect any browser
frontend at its intended screen sizes. A passing command does not establish
visual quality, external service delivery or production readiness.

Local generation does not automatically deliver a pull request. See
[Git publication](scm.md#publish-a-verified-change) for explicit publication.
