---
hide:
  - navigation
  - toc
  - footer
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

<div class="forge-docs-home" markdown="1">

<div class="forge-docs-hero" markdown="1">

<div markdown="1">

# <span>Build a product.</span> <span>Keep the evidence.</span>

Define the customer journey, give an agent a bounded task, and review what it actually built. firefly forge brings product decisions, source changes, verification and delivery into one recorded workflow.

[Set up forge](installation.md){ .md-button .md-button--primary }
[Build your first product](quickstart.md){ .md-button }

Start with [**pyfly**](https://fireflyframework.org/frameworks/python/) or [**larafly**](https://fireflyframework.org/frameworks/php/). Add a browser frontend when the product needs one; API and service projects follow the same delivery process.

</div>

<div class="forge-docs-hero-art docs-scene" data-docs-scene data-renderer="fallback" aria-describedby="scene-demo-note" markdown="1">

<div class="docs-scene__viewport" data-scene-viewport markdown="1">
<div class="docs-scene__fallback" markdown="1">

![Firefly's factory stations and connected production lanes](assets/forge-factory-light.svg){ .forge-docs-art-light }

![Firefly's factory stations and connected production lanes in dark appearance](assets/forge-factory-dark.svg){ .forge-docs-art-dark }

</div>
</div>

<div class="docs-scene__controls">
<span class="docs-scene__status" data-scene-status aria-live="polite">Illustration · no live activity</span>
<button type="button" data-scene-toggle aria-pressed="false" disabled>Loading…</button>
</div>

</div>

</div>

## What do you want to do?

<div class="forge-docs-routes" markdown="1">

<div markdown="1">

### :material-map-outline: Define the product

Capture journeys, features, integration contracts and acceptance criteria before implementation.

- [Work through the product wizard](product-definition.md)
- [Bring in an existing project](as-is-handoff.md)
- [Review an architecture proposal](product-definition.md#review-an-architecture-proposal)
- [Review wireframes](design-review.md)
- [Deliver an incremental feature](product-lifecycle.md)

</div>

<div markdown="1">

### :material-source-branch: Build and review

Choose the coding provider and its tools, run checks, and inspect the source changes and evidence.

- [Choose an agent and model](agents.md)
- [Configure the execution environment](sandbox.md)
- [Review browser journeys](browser-qa.md)
- [Work from repository conversations](repository-workflow.md)

</div>

<div markdown="1">

### :material-server-outline: Run the factory

Prepare identity, storage and delivery access. Keep execution, recovery and application health observable.

- [Sign in and manage project access](identity.md)
- [Separate API and worker processes](runtime-roles.md)
- [Deploy a verified product](deployment.md)
- [Continue interrupted work](recovery.md)

</div>

</div>

## Follow one change through the factory

<div class="forge-docs-process" markdown="1">

1. **Define the expected behavior.** Save the customer journey and measurable acceptance criteria. [Product definition](product-definition.md)
2. **Prepare the task.** Choose the repository, stack, provider, policies and execution limits. [Build, maintain and test](workflows.md)
3. **Implement and verify.** Review the diff and the actual results of commands and browser checks. [Agent tools](agent-tools.md)
4. **Review and deliver.** Check the verified source, publish explicitly, then deploy to an authorized target. [Delivery assurance](assurance.md)
5. **Observe and maintain.** Inspect application health and link follow-up work to its evidence. [Application operations](application-operations.md)

</div>

## Check what each result establishes

A completed agent response, a passing test, a visual review and a successful deployment establish different things. Use the [capabilities guide](capabilities.md) to understand those boundaries and the [API reference](api.md) to automate supported actions.

New to the factory? [Find your next step](guide.md) connects the guides by role and shows where to check each result.

</div>

<p id="scene-demo-note" class="docs-scene-note">The home-page animation is a demonstration. It reads no run data and makes no model calls. <a href="assets/docs-scene/THIRD_PARTY_NOTICES.txt">Illustration licenses and notices</a>.</p>
