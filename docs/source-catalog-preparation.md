---
description: Stream verification source identity without blocking the runtime or retaining a project-wide map.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Preparing verification source identity

Large public source trees can be hashed without loading each file or a project-wide map into runtime memory. `prepare_verification_source` runs the streamed catalog capture on a worker thread. Its authority callback runs on the original event loop with the original caller context, so runtime lease renewal stays responsive while file hashing and external sorting proceed.

The returned `source_digest` uses the existing `forge-verification-source-v1` canonical JSON identity. File hashes, modes, directory modes, Unicode names and the public filename selection retain their existing meanings. The opaque catalog reference and authenticated metadata pages remain outside the workspace. This does not change existing request or receipt serialization.

The caller supplies a `SourceCatalogBudget` with an optional total source/storage budget, free-space floor, deadline and bounded processing pages. A project does not acquire an implicit 100 MB or 10,000-entry limit through this interface. The earlier standalone large-source and entry-count proofs remain separate from the small synthetic integration tests.

## Admission sequence

1. Hold the native exclusive-workspace ownership used for the operation. This helper does not acquire ownership, grant permissions or isolate a workspace from other operating-system writers.
2. Outside every PyFly caller unit, prepare the source catalog with a current-authority callback. The helper rejects a live caller transaction before filesystem work and never transfers its transaction context to the worker thread.
3. Still outside a writer unit, call `prepared.revalidate(assert_current=...)`. This validates all authenticated metadata pages and original file/directory identities without rereading payloads. The last authority await precedes the final metadata pass; the pass does not rebase its original identities.
4. Keep exclusive workspace ownership through the brief native admission. Recheck the native actor, policy, scope, cancellation and ownership inside that unit, then use `verification_source(workspace, prepared=prepared)` or `check_verification_cut(..., prepared=prepared)` for the constant-size root/catalog metadata fence. Record the original digest in the existing intent/receipt.

The constant-size fence relies on the ownership contract spanning the preparation and admission. It is not a replacement for the full off-loop revalidation, and it does not independently detect every descendant edit after that revalidation. A caller that cannot retain that ownership must not select the prepared path.

Cancellation signals the worker cooperatively at bounded processing checks. The caller waits for descriptor closure and partial-catalog cleanup before cancellation returns. Authority failures are propagated, not converted into a digest.

## Wired worker handoff path

`VerificationHandoffs.execute` prepares outside writer units while retaining Engine's repository execution slot and publication lock. The existing lock serializes checkpoints with the paused verification operation; the runtime execution lease remains mandatory. Each authorization boundary revalidates public metadata off-loop and checks current execution authority. A private task-scoped cut makes synchronous Journal checks use the brief fence, without changing immutable intent or response formats.

The path covers frozen command admission, command receipts, completion, resume and next-provider admission in its new task. Journal prepares outside its writer unit when that later task has no prepared context. It rechecks the native lease and actor before the final source/policy fence following write awaits. Unknown command effects retain their reconciliation boundary and do not replay.

Revalidation compares all selected public membership through externally sorted pages. Excluded check caches can change directory timestamps without changing application source identity. File identities and modes remain bound to the original catalog; added/deleted public paths refuse. Metadata revalidation does not reread payloads. The final constant-size fence relies on exclusive workspace ownership; it does not independently detect arbitrary descendant or operating-system edits after that pass.

Cancellation drains the worker thread and pending authority callback before releasing the publication lock. Temporary metadata catalogs are removed on success, refusal or cancellation. Catalog references grant no actor, source-edit, command or continuation authority.

## Remaining boundaries

Explicit operator `candidates`/`adopt` still use synchronous legacy source capture, including the adoption writer callback. That path has not been migrated or claimed scan-free. `Assurance.review` retains separate archive/extraction and review-workspace manifest limits. This worker-source change does not make every verification or review workflow elastic.

Engine and Store are unchanged. The changed verification model, Journal, handoff code and both catalog helpers participate in the future admission closure and require separate final-source native qualification before adoption. Existing v8 evidence is not rebound to this overlay.
