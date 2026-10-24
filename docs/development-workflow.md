---
description: Configure development checkpoints, inspect saved commits and draft reviews, and distinguish work in progress from verified final delivery.
---

<!-- Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 -->

# Save development progress

A development checkpoint records work in Git before final delivery. Use checkpoints to inspect progress without treating an unfinished run as ready for release.

## Choose a workflow for new runs

Open **Product projects**, select a project, and find **Development workflow**.

1. Enable **Save development checkpoints**.
2. Choose how many completed implementation rounds should pass between checkpoints, from 1 to 20. One round is a completed coding turn. Individual tool calls and elapsed time do not count as rounds.
3. Set a maximum of 1 to 50 checkpoints per run, including the reserved final verified checkpoint. A maximum of 1 saves only that final checkpoint; choose at least 2 for intermediate progress. Optionally save at a safe pause after the worker has stopped.
4. Choose **Local Git checkpoints** or **Push to a draft review**.
5. Select **Save workflow**. Studio waits for the native API to acknowledge the exact project, policy and request before reporting a saved change.

Existing projects start with checkpoints disabled. Schedule, maximum and delivery changes apply to new runs; a run already in progress keeps those recorded settings. Turning this off also stops further checkpoints for existing runs. Saved commits remain.

A factory administrator with project membership-management permission can configure the workflow. Remote delivery also requires current release permission and an adapter that supports draft updates for every selected repository. The initial remote adapters support GitHub draft pull requests and GitLab draft merge requests. Other providers retain local checkpoints. Studio reads these capabilities from the server rather than inferring support from a provider name.


<figure class="forge-shot">
<picture class="forge-shot__link theme-light">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-settings-mobile-light.png">
  <img src="../assets/screenshots/development-workflow-settings-desktop-light.png" alt="Development workflow schedule with completed rounds, checkpoint maximum and local or draft delivery" width="1440" height="900" loading="lazy">
</picture>
<picture class="forge-shot__link theme-dark">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-settings-mobile-dark.png">
  <img src="../assets/screenshots/development-workflow-settings-desktop-dark.png" alt="Development workflow schedule with completed rounds, checkpoint maximum and local or draft delivery" width="1440" height="900" loading="lazy">
</picture>
<figcaption>Controlled API fixture: choose the checkpoint schedule and delivery mode. The maximum includes a reserved final checkpoint; turning checkpoints off stops further checkpoints without removing saved commits.</figcaption>
</figure>

If a save is refused or its response is lost, Studio reads the current policy and reports the outcome. It does not automatically repeat the write. Refresh and review the saved settings before making another change.


<figure class="forge-shot">
<picture class="forge-shot__link theme-light">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-actions-mobile-light.png">
  <img src="../assets/screenshots/development-workflow-actions-desktop-light.png" alt="Workflow save and refresh actions remain visible above the portal footer" width="1440" height="900" loading="lazy">
</picture>
<picture class="forge-shot__link theme-dark">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-actions-mobile-dark.png">
  <img src="../assets/screenshots/development-workflow-actions-desktop-dark.png" alt="Workflow save and refresh actions remain visible above the portal footer" width="1440" height="900" loading="lazy">
</picture>
<figcaption>Controlled API fixture: review the settings and use Save workflow. Studio waits for the native acknowledgment and reads back the policy; a lost response does not trigger another write.</figcaption>
</figure>

## Inspect large changes

Large changes are retained as private artifacts with manifest and content hashes, rather than embedded in each journal entry or prompt. Read the file index and only the needed content windows through the authenticated API, typed SDK or advanced CLI. The project's optional `diff_budget` separates storage volume, time and preview controls. When `max_bytes` is `null`, this budget imposes no fixed total-patch ceiling; its free-space floor and deadline still apply. See [Inspect large source changes](git-diff-archive.md).

Each checkpoint records the exact delta from its previous factory commit while retaining the original baseline in its chain. File counts describe the complete artifact; a labeled file-name preview does not reduce component-scope checks or replace the full index.

## Ask for a checkpoint from GitHub

In the issue linked to the run, write a normal request such as “@forge, save the current progress as a draft pull request.” The configured communication model interprets the message; the server selects the authenticated run linked to that issue. An ambiguous reference requires clarification. You do not need to copy a run identifier or write a command.

This action needs a separate `checkpoint` grant in the current API-observed GitHub account mapping, current project release permission, and the repository's `publish` channel permission. Existing status and answer grants do not include it. The run must also have checkpoints enabled in its recorded policy, a closed implementation unit and an exact review of the current source. The server observes the current source and HEAD without staging or creating a commit; the native owner rechecks that source before preparing its checkpoint. A pause does not need an earlier automatic source-tree receipt. A conversation cannot enable the policy, adopt a legacy run or give itself permission.

The reply distinguishes a queued request from a confirmed commit or draft review. The native execution owner performs the Git operation and records its receipts. The original task status and pending checks remain unchanged. If a queue acknowledgment is lost, forge retains the uncertain admission and does not send it again; inspect the exact checkpoint request and its delivery evidence before taking another action.

Runs started before checkpointing was enabled require a separate native administrator review of the partial output. A GitHub account mapping never inherits administrator authority for that review.

## Read saved progress

Open a run and select **Plan**. **Saved development progress** shows the server's checkpoint receipts, including the commit, source tree, branch, implementation task, attempt, changed-file count and save time. Commit and tree identifiers remain complete on narrow screens.

| State | Meaning |
| --- | --- |
| Saved locally | A local checkpoint was recorded; remote delivery was not requested. |
| Remote delivery in progress | The local commit exists. A completed remote delivery has not been confirmed. |
| Draft review updated | The server recorded a draft review update. Use its review link to inspect the work. |
| Remote outcome needs review | The remote result is uncertain. Inspect retained evidence before taking another action; this view sends no replacement push. |
| Review marked ready | The server recorded that the review was marked ready. Run completion, final checks and approval remain separate records. |


<figure class="forge-shot">
<picture class="forge-shot__link theme-light">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-history-mobile-light.png">
  <img src="../assets/screenshots/development-workflow-history-desktop-light.png" alt="Checkpoint history showing the recorded commit, source tree, draft status and review link" width="1440" height="900" loading="lazy">
</picture>
<picture class="forge-shot__link theme-dark">
  <source media="(max-width: 640px)" srcset="../assets/screenshots/development-workflow-history-mobile-dark.png">
  <img src="../assets/screenshots/development-workflow-history-desktop-dark.png" alt="Checkpoint history showing the recorded commit, source tree, draft status and review link" width="1440" height="900" loading="lazy">
</picture>
<figcaption>Controlled API fixture: a draft checkpoint preserves the full commit and source-tree identifiers. Draft progress is separate from final checks, approval and run completion; these screenshots do not prove a live push or remote review.</figcaption>
</figure>

The timeline is read-only. It does not interrupt an active worker, add execution consent, change a run's budget or expose a manual checkpoint command. Missing access, malformed receipts and receipts for a different run are shown as unavailable rather than displayed as verified progress.

## Validation boundary

The Studio component and browser fixtures use envelopes serialized by the backend's versioned models. They verify presentation, scope checks, permissions and write acknowledgment. These controlled fixtures do not prove a live push, remote draft update or completed run; those require the backend and provider acceptance evidence for the integrated release.

## Preserve partial work from a failed run

A failed implementation can contain useful source changes without being ready for release. For an older run created without a checkpoint policy, a native administrator can explicitly adopt that one failed unit after enabling the project's development workflow. This action does not change the run's original frozen policy, mark its implementation task complete, answer a question, restart the coding provider or add execution budget.

Review `GET /api/v1/runs/{run_id}/git-deliveries/adoption-candidate`. The candidate binds the current failed feedback, canonical task and retained completed-turn evidence, full public source, Git HEAD and index, current project policy and journal head. It does not claim an operating-system exit or successful implementation. An active worker, unresolved tool or command effect, changed source, revoked permission or uncertain Git delivery holds adoption.

Submit the reviewed digests and the three explicit acknowledgments in `GitDeliveryInputV1.explicit_adoption` through the existing `POST /api/v1/runs/{run_id}/git-deliveries` endpoint. The request grants one checkpoint under finite consent. Its native owner records the adoption before Git effects; repeated reads or the same request identifier return the retained outcome. An unknown commit or remote outcome is never automatically replayed.

The commit and draft title carry **UNVERIFIED**, and the receipt identifies `verification_status: partial_unverified`. The selected communication writer receives facts stating that the implementation reported failure and verification has not run; its existing review and no-fallback rules still apply. This draft cannot be promoted to a final ready review through the failed run. The existing saved-progress view remains read-only; a dedicated adoption form is not part of this API slice.

Use [Verify a saved checkpoint](checkpoint-verification.md) to select its exact confirmed remote revision through a fresh native creation request. The separately authorized run keeps the same repository, project and component, uses `operation: test` and `publish: false`, and retains its own reviewed commands and sandbox policy. That new verification run retains its own identity and receipts; it does not rewrite the original failed result or prove requirements that its checks do not cover. Keep independent database, browser, restart and product acceptance evidence separate.
