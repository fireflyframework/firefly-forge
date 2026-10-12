<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->
# Create a PyFly service

PyFly is the primary Python backend profile. forge gives the agent the current
Firefly contracts: typed models, controllers, dependency injection, asynchronous
services and the Starlette web adapter. PyFly is distributed through its
official GitHub Releases; do not assume a similarly named PyPI package is the
framework.

## Prerequisites

Install Python 3.13+, uv and the coding provider on the forge server. Register a
repository and confirm that it can be cloned. Choose **PyFly** in New product
and **API only** for a service exercise. Add a browser frontend when your brief
actually needs one.

## Use a concrete product brief

```text
Create a PyFly service for reserving meeting rooms. Expose endpoints to list
rooms, reserve a room for a UTC interval and cancel a reservation. Reject
overlapping reservations with a conflict response. Validate intervals and
unknown room IDs. Keep persistence in memory for this exercise. Use native
PyFly controller and service contracts, typed models, pytest behavior tests,
ruff, type checking and a make check target. Include a launch command and
example API requests in the README.
```

Add `[["make", "check"]]` as verification commands and start the run. You should
be able to identify tests for overlapping intervals, invalid input and unknown
rooms in the resulting diff. If those tests are missing, update the brief and
retry rather than accepting a generic completion report.

## Verify the product

Read **Verification** and execute the generated project's launch instructions.
Send valid, overlapping and invalid reservation requests. The product's README
must identify its actual port, paths and environment; forge does not assign a
universal port to generated applications.

The selected profile is an instruction contract, not a promise that every
possible PyFly feature or external integration is verified. Runtime catalog
validation and a specific run's evidence are separate facts. See
[capabilities](capabilities.md).
