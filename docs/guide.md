<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Find your next step

For an illustrated case from connection through run evidence and recovery, follow the [Studio console tour](console-tour.md).

Choose the path that matches the work in front of you. You can join an existing product at any stage; a new feature does not require recreating its original definition.

## Define or change a product

**Prepare:** a customer problem, a product owner and the repositories involved. For an existing system, start with its current behavior and known gaps.

1. [Review the existing project](as-is-handoff.md), or [define a new product](product-definition.md).
2. Map each journey to features, expected behavior, failure paths and acceptance checks. Record API and system processes for products without a frontend.
3. [Review an architecture proposal](product-definition.md#review-an-architecture-proposal). Choose [pyfly](https://fireflyframework.org/frameworks/python/) or [larafly](https://fireflyframework.org/frameworks/php/), identify components and integration contracts, and [apply the product's brand and layout rules](product-design.md).
4. [Review wireframes](design-review.md) when the product has a visual interface. Attach the references to the definition.
5. Save the product revision. [Update feature contracts](product-lifecycle.md), then request a [product build](workflows.md#build-a-product) against that revision.

**Check the result:** the saved revision contains the agreed journeys and acceptance criteria. Saving a definition or applying an assistant proposal does not approve code or start a run.

## Implement and verify a change

**Prepare:** repository access, a configured provider, an execution environment and a bounded task. [Your first product](quickstart.md) walks through a small API example.

1. [Connect the repository](scm.md) and [choose a coding agent](agents.md).
2. Select [task-specific models](model-profiles.md), [agent roles](agent-roles.md), [policies](policies.md) and [runner limits](sandbox.md).
3. Request the change from Studio or an authorized [repository conversation](repository-workflow.md).
4. Inspect the run's activity, diff, command output and verification evidence. Follow the [browser review workflow](browser-qa.md) for visual work.
5. Review before [publication and deployment](deployment.md). Resolve failures against their recorded evidence; do not hide them behind a retry.

**Check the result:** the retained verification belongs to the exact source being reviewed. A successful model response alone does not establish that the change works.

## Operate the factory and its products

**Prepare:** dedicated forge and Flycanon databases, private retained storage, an identity provider and the deployment boundaries for the installation.

1. Prepare [sign-in](identity.md), [install the factory](installation.md), then [manage users](user-administration.md).
2. Choose the [API and worker topology](runtime-roles.md). Verify shared storage and ownership before adding replicas.
3. Verify the included [Flycanon knowledge service](knowledge.md) and its project scopes. Configure source-control applications and provider access.
4. [Deploy the product](deployment.md), then inspect [health and maintenance](application-operations.md).
5. Keep [backups and operational records](operations.md). Use [recovery](recovery.md) for interrupted work or uncertain external effects.

**Check the result:** verify the actual service, authorized user, worker, external integration and application health separately. A rendered cloud recipe is a configuration preview; it does not prove a deployment.

## Use the documentation

- **Start** covers setup, the first change and verification boundaries.
- **Products** covers journeys, architecture, design and incremental delivery.
- **Agents** covers provider access, roles, tools, policies and execution environments.
- **Integrations** covers repositories, channel conversations, knowledge and delivery pipelines.
- **Operations** covers identity, deployment, health and recovery.
- **Reference** describes architecture, API contracts, releases and contribution rules.

Search uses the guide titles and body text. On a phone, open the navigation menu to browse sections and use **Table of contents** to move within a long guide. Light and dark appearance use the same reading layout.
