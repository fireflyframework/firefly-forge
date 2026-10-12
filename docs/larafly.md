<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Create a LaraFly application

LaraFly is the primary PHP backend profile. Forge uses the Laravel-native
Firefly contracts, including service composition, attributes, repositories and
typed models. The agent must inspect the official skeleton and CLI stubs before
using exact namespaces; it must not invent framework APIs.

## Prerequisites

Install PHP 8.3+ and Composer on the Forge server, together with the extensions
required by the selected Laravel release. Register a Git repository and choose
a ready agent. Select **LaraFly**, then a frontend or **API only**.

## Use a concrete product brief

```text
Create a LaraFly API for a support queue. People can create a ticket, list
tickets by status and assign an open ticket to an operator. Validate title and
description, reject assigning a closed ticket, and return a 404 for an unknown
ticket. Use native LaraFly service composition and Laravel validation. Use
SQLite for this local exercise, behavioral Pest or PHPUnit tests, Laravel Pint
and PHPStan or Larastan. Configure Composer scripts named test, lint and
typecheck and a make check target that runs all three. Document exact setup,
migration, startup and example requests.
```

Add `[["make", "check"]]` as verification commands. Watch the timeline, then
inspect the real verification output and generated source. The agent should use
the current scaffold non-interactively without forcing an overwrite or making
an initial Git commit.

## Verify the product

Follow the generated README's dependency, database and startup commands in a
dedicated local environment. Exercise creation, assignment, invalid input and
unknown IDs. Do not point a generated application's migrations at a shared or
production database.

A LaraFly profile selection does not establish readiness of an arbitrary
database, external provider or production deployment. Keep those checks separate
from the run's build and behavior evidence.
