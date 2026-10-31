---
description: Follow five stages from workspace setup and product decisions to a reviewed delivery and operation in firefly forge.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Find your next step

Follow one delivery through five stages: **Prepare workspace → Define or adopt product → Plan delivery → Build and review → Deliver and operate**. Start at the stage that matches your work. An existing product keeps its definition and history; a new feature changes the relevant behavior and acceptance checks.

The example is an inventory product that reserves available stock and explains refused requests. It can begin as an API with no frontend. A warehouse web application can join later as another component of the same product.

Use [factory journeys](factory-journeys.md) for the detailed steps and [the Studio console tour](console-tour.md) for illustrated navigation. A product project groups the pieces you own; a component names one piece and its repository checks. A campaign coordinates a delivery across selected components. Each run retains the work and evidence for one repository or component.

## Choose where to enter Studio

| Section | Use it when | Keep the result and continue |
| --- | --- | --- |
| [Factory](factory-journeys.md#build-and-review) | You want to follow work already requested or inspect a run. | Read its activity and evidence, then review the source or resolve its recorded hold. |
| [Product projects](factory-journeys.md#2-configure-the-product-project) | You need to group repositories and components or inspect a campaign. | Save the topology; open its product contract or select the delivery scope. |
| [Product definition](product-definition.md) | You need to agree on behavior or change a feature. | Save the reviewed revision, then plan a run or campaign against it. |
| [Repositories](scm.md) | You need to inspect registered source and repository settings. | Confirm access and checks, then use that repository in a project or run. |
| [Start a run](workflows.md) | You have one specific build, maintenance or test request. | Review its source and product revision before submitting, then follow it in Factory. |
| [Knowledge](knowledge.md) | You need scoped references or to inspect retrieval readiness. | Check the configured service and citations before work depends on them. |
| [Agents & skills](agent-roles.md) | You need to review role instructions, skills and tool selections. | Save permitted changes before a new run freezes its execution settings. |
| [Deployment](deployment.md) | You have reviewed source and an intended delivery target. | Inspect the deployment's own outcome and post-deployment checks. |
| [Applications](application-operations.md) | You need health observations or to manage an incident. | Save observations and request a focused maintenance change when needed. |
| [Administration](user-administration.md) | You are a factory administrator setting up access or installation services. | Configure the required connection or permission, then return to the product work. This section is administrator-only. |

## 1. Prepare workspace { #prepare-workspace }

Give the team an authorized place to work. Start with the factory address, the people who will use it and the source repositories.

1. [Install the factory](installation.md) if needed, [sign in](identity.md) and assign [access](user-administration.md).
2. In **Connections → Repositories**, [connect the source](scm.md). Configure the permitted [coding provider](agents.md), [models](model-profiles.md), [roles](agent-roles.md), [policies](policies.md) and [runner](sandbox.md).
3. Follow the current [knowledge setup](knowledge.md) for this installation and verify a scoped search. Fresh setup and an existing Flycanon installation can have different configuration paths; saved bindings and in-flight runs keep their recorded service.

Continue to product decisions when the intended user can access the repository and the required execution and knowledge services have readiness evidence.

<span id="define-or-change-a-product"></span>

## 2. Define or adopt product { #define-or-adopt-a-product }

Agree on the behavior the customer needs. Bring a problem, a product owner and either the existing source or the intended architecture.

1. In **Product projects**, configure the [project and its components](factory-journeys.md#2-configure-the-product-project), using one repository or several.
2. [Review existing source](as-is-handoff.md) for adoption, or [define a new product](product-definition.md). Map journeys to features, failure behavior and acceptance checks. API-only products use customer and system processes without adding a visual interface.
3. Review [architecture](product-definition.md#review-an-architecture-proposal), [brand and layout rules](product-design.md) and [wireframes](design-review.md) when needed. Use **Product and design system** from the project overview to open the product contract.

The result is a saved product revision with agreed behavior and component responsibilities. For an incremental feature, [update the relevant feature contracts](product-lifecycle.md) in that product, then plan its delivery.

## 3. Plan delivery { #plan-delivery }

Choose a specific change against the saved product revision. Identify the affected components, their dependencies and the checks that will establish acceptance.

Use **Start component run** for one component, or the explicit [campaign API, SDK, CLI or MCP operation](project-campaigns.md) for work spanning components. An authorized [repository conversation](repository-workflow.md) can request supported work through its enabled commands. [Build, maintain and test](workflows.md) describes the available run types.

Once accepted, the run or campaign records the selected scope, source and execution settings. Open its saved plan and evidence before following implementation; later configuration edits do not change those inputs.

<span id="implement-and-verify-a-change"></span>

## 4. Build and review { #build-and-review }

Implement the requested change as source you can inspect. Open its run in **Factory** and review the activity, plan, diff, command output and verification evidence. For visual work, follow [browser review](browser-qa.md). [Your first product](quickstart.md) follows a small API example.

Resolve failures from their recorded evidence. Use [recovery](recovery.md) for interrupted work or an uncertain external outcome, keeping the original request identity when a response is lost. Review the [campaign report](project-campaigns.md#read-a-report-in-studio) when several components contribute to the delivery.

Continue when the exact source under review has the required checks and reviews. Keep that checkpoint with the delivery decision; a model response alone does not establish acceptance.

<span id="operate-the-factory-and-its-products"></span>

## 5. Deliver and operate { #deliver-and-operate }

Bring the reviewed source checkpoint and an explicitly configured delivery target. [Publish the change](scm.md#publish-a-verified-change) when requested, review its actual remote result, then follow [deployment](deployment.md) for the accepted source and its post-deployment checks.

Use [application health and maintenance](application-operations.md) to observe the running product. Keep [backups and operational records](operations.md), and use incidents to request the next focused maintenance change. Operators can review [API and worker topology](runtime-roles.md) when scaling the factory.

Keep each delivery outcome with its evidence. A maintenance run needs its own delivery decision; after an accepted fix, collect fresh observations before resolving the incident. New product requirements return to stage 2, while an implementation change with agreed scope can start from stage 3.

## Use the documentation

- **Start** covers setup, the first change and verification boundaries.
- **Products** covers journeys, architecture, design and incremental delivery.
- **Agents** covers provider access, roles, tools, policies and execution environments.
- **Integrations** covers repositories, channel conversations, knowledge and delivery pipelines.
- **Operations** covers identity, deployment, health and recovery.
- **Reference** describes architecture, API contracts, releases and contribution rules.

Search uses the guide titles and body text. On a phone, open the navigation menu to browse sections and use **Table of contents** to move within a long guide. Light and dark appearance use the same reading layout.
