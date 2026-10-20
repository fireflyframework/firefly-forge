/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { createHash } from "node:crypto";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough as capture } from "./walkthrough-capture";
import { record as learningRecord } from "./learning-record-fixture";
import {
  fixture as setupRunCreation,
  open as openRunCreation,
  submit as submitRunCreation,
  markers as runCreationMarkers,
  retainedRunCreation,
} from "./run-creation-cancellation-fixture";
import { setup as setupOriginalResult } from "./agent-checkpoint-recovery-fixture";
import { verifiedReport } from "./campaign-report-fixture";
import { setupDelivery } from "./campaign-delivery-browser-fixture";
import workerContract from "./worker-dialogue-contract-fixture.json" with { type: "json" };
import {
  setup as setupCampaignHistory,
  selectCampaign as selectCampaignHistory,
  reviewSave as reviewCampaignSave,
  native as campaignArchiveFixture,
  digest as archiveFixtureDigest,
} from "./campaign-report-history-fixture";

// Route fixtures exercise Studio presentation; no provider or knowledge service runs.
const repository = {
  id: "1".repeat(32),
  name: "Fixture Inventory API",
  provider: "git",
  url: "/local/fixture-inventory",
  default_branch: "main",
};
const learningRepository = {
  ...repository,
  id: learningRecord.envelope.context.repository_id,
  name: "Fixture Learning Inventory",
  url: "/local/fixture-learning-inventory",
};
const failureCandidate = {
  ...learningRecord,
  id: "9".repeat(32),
  state: "candidate",
  kind: "observed_failure",
  observed_at: "2026-10-14T12:05:00Z",
  verified_at: undefined,
  envelope: {
    ...learningRecord.envelope,
    type: "observed_failure",
    correlation_id: "8".repeat(32),
    run_id: "7".repeat(32),
    question: "Observe a rejected reservation check",
    statement:
      "The reservation assertion failed in the recorded source context. This observation does not establish a reusable solution.",
    context: {
      ...learningRecord.envelope.context,
      query_digest: "5".repeat(64),
    },
    citations: [],
    receipts: [
      {
        kind: "receipt",
        key: "fixture-failure:verify",
        sequence: 8,
        digest: "4".repeat(64),
      },
    ],
  },
};
const run = {
  id: "2".repeat(32),
  repository_id: repository.id,
  brief: "Implement stock reservation and reject over-reservation",
  backend: "pyfly",
  frontend: "none",
  agent: "codex",
  status: "needs_reconciliation",
  events: [
    {
      message: "Verification command returned",
      created_at: "2026-10-13T15:00:00Z",
      actor_id: "fixture-operator",
      argv: ["uv", "run", "pytest"],
      exit_code: 2,
      output: "Reservation rejected: requested 8, available 5",
    },
  ],
  verification: [
    {
      argv: ["uv", "run", "pytest"],
      exit_code: 2,
      duration_ms: 1530,
      output: "Reservation rejected: requested 8, available 5",
      stderr:
        "Expected reservation response differs from the fixture assertion.",
      actor_id: "fixture-operator",
    },
  ],
  changed_files: ["src/check.py"],
  diff: "diff --git a/src/check.py b/src/check.py\n--- a/src/check.py\n+++ b/src/check.py\n@@ -10,2 +10,2 @@ def check():\n-    return False\n+    return True\n     # reservation bound",
  artifacts: [
    {
      name: "changes.zip",
      size: 2048,
      url: `/api/v1/runs/${"2".repeat(32)}/artifacts/changes.zip`,
    },
  ],
};
const architecture = {
  backend: "pyfly",
  frontend: "none",
  components: [
    {
      name: "Inventory",
      layer: "domain",
      responsibility: "Reject over-reservation",
    },
  ],
  decisions: ["Use transaction boundaries"],
  nonfunctional_requirements: [],
  reviewed: false,
};

for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"]) {
    test(`guided fixture journey ${device} ${theme}`, async ({ page }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      let product: unknown = null;
      let admitted = false;
      const creation = retainedRunCreation(run.id);
      let searchSettings: Record<string, unknown> = {
        provider: "brave",
        endpoint: "https://api.search.brave.com/res/v1/web/search",
        revision: null,
        configured: false,
        max_results: 10,
        live_search_verified: false,
      };
      let searchTests = 0;
      const learningReads: string[] = [];
      let invalidation: { request_id: string; reason: string } | null = null;
      let invalidationWrites = 0;
      let receiptReady = false;
      const invalidationReads: string[] = [];
      let learningSettings = {
        version: 1,
        revision: "a".repeat(32),
        digest: "b".repeat(64),
        settings: {
          enabled: true,
          projected_retrieval: false,
          max_age_hours: 72,
          max_candidates: 8,
          max_lookup_calls: 30,
          max_repeated_attempts: 2,
        },
      };
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (path.endsWith("/learning") || path.includes("/learning/records/"))
          learningReads.push(request.method() + " " + path);
        let json: unknown = {};
        if (path.endsWith(`/${learningRepository.id}/learning`))
          json = [
            {
              ...learningRecord,
              state: receiptReady ? "invalidated" : "verified",
            },
            failureCandidate,
          ];
        else if (
          path.endsWith(`/learning/records/${learningRecord.id}/invalidate`)
        ) {
          expect(request.method()).toBe("POST");
          invalidationWrites++;
          invalidation = request.postDataJSON();
          return route.abort("failed");
        } else if (
          path.includes(`/learning/records/${learningRecord.id}/invalidations/`)
        ) {
          expect(request.method()).toBe("GET");
          expect(invalidation).not.toBeNull();
          expect(path).toBe(
            `/api/v1/learning/records/${learningRecord.id}/invalidations/${invalidation!.request_id}`,
          );
          invalidationReads.push(path);
          if (!receiptReady)
            return route.fulfill({
              status: 404,
              json: { detail: "Learning invalidation request not observed" },
            });
          json = {
            event_id: "2".repeat(32),
            record_id: learningRecord.id,
            repository_id: learningRepository.id,
            run_id: learningRecord.envelope.run_id,
            request_id: invalidation!.request_id,
            kind: "invalidation",
            event_state: "invalidated",
            current_record_state: "invalidated",
            reason: invalidation!.reason,
            actor_id: "f".repeat(32),
            created_at: "2026-10-15T01:30:00Z",
            sequence: 2,
            digest: "3".repeat(64),
          };
        } else if (
          path.endsWith(`/learning/records/${learningRecord.id}/projection`)
        )
          json = {
            id: "1".repeat(32),
            record_id: learningRecord.id,
            state: "verified",
            workspace_id: "fixture-learning-inventory",
            source_id: "fixture-reservation-source",
            source_status: "ingested",
            content_sha256: "2".repeat(64),
            target: { revision: "3".repeat(32) },
            citations: [
              {
                source_id: "fixture-reservation-source",
                chunk_id: "fixture-reservation-chunk",
              },
            ],
          };
        else if (path.endsWith("/learning/policy")) {
          if (request.method() === "PUT") {
            learningSettings = {
              ...learningSettings,
              revision: "c".repeat(32),
              digest: "d".repeat(64),
              settings: request.postDataJSON(),
            };
          }
          json = learningSettings;
        } else if (path.endsWith("/search/configuration")) {
          if (request.method() === "PUT") {
            const values = request.postDataJSON();
            searchSettings = {
              provider: values.provider ?? searchSettings.provider,
              endpoint:
                values.provider === "tavily"
                  ? "https://api.tavily.com/search"
                  : "https://api.search.brave.com/res/v1/web/search",
              revision: "d".repeat(32),
              configured: true,
              max_results: 10,
              live_search_verified: false,
              include_domains: values.include_domains ?? [],
            };
          }
          json = searchSettings;
        } else if (path.endsWith("/search/test")) {
          searchTests++;
          json = {
            provider: searchSettings.provider,
            live_search_verified: false,
            result_count: 0,
          };
        } else if (path.endsWith("/repositories"))
          json = [repository, learningRepository];
        else if (path.endsWith("/providers"))
          json = [
            {
              id: "codex",
              name: "Codex fixture",
              ready: false,
              detail: "Fixture provider; no real login or model request",
            },
          ];
        else if (
          path.endsWith("/scm/connections") ||
          path.endsWith("/deployment-targets") ||
          path.endsWith("/assistant/conversations") ||
          path.endsWith("/projects")
        )
          json = [];
        else if (path.endsWith("/knowledge/configuration"))
          json = {
            enabled: true,
            endpoint: "https://knowledge.example.test",
            tenant_id: "fixture",
            framework_workspace: "frameworks",
            required: true,
            auto_memory: true,
            api_key_configured: false,
            agent_token_configured: false,
            top_k: 5,
            readiness: {
              state: "not_configured",
              detail: "Fixture only; retrieval has not been verified",
            },
          };
        else if (path.endsWith("/knowledge/explore"))
          json = {
            workspace_id: "fixture-inventory",
            scope: "project",
            sources: [],
            knowledge: [],
            offset: 0,
            limit: 20,
            totals: { sources: 0, knowledge: 0 },
            browsing_available: true,
            warnings: [],
          };
        else if (
          path.endsWith("/product") ||
          path.endsWith("/product/conditional")
        ) {
          if (request.method() === "PUT")
            product = {
              repository_id: repository.id,
              revision: 1,
              definition: request.postDataJSON().definition,
              digest: "a".repeat(64),
              created_at: "2026-10-13T15:00:00Z",
            };
          json = product;
        } else if (path.endsWith("/brand")) json = null;
        else if (path.endsWith("/architecture/propose"))
          json = {
            method: "rule_based",
            recommendation: {
              backend: "pyfly",
              frontend: "none",
              rationale: "API-only reservation contract",
              alternatives: [],
            },
            architecture,
          };
        else if (path.endsWith("/skills"))
          json = {
            revision: "fixture-policy-revision",
            digest: "fixture-policy-catalog",
            skills: [
              {
                id: "verification",
                name: "Behavior checks",
                description: "Exercise valid and rejected reservations",
                kind: "recommendation",
                default: true,
              },
            ],
          };
        else if (path.endsWith("/policies"))
          json = path.includes("/repositories/")
            ? { skills: ["verification"] }
            : {
                revision: "fixture-policy-revision",
                digest: "fixture-policy-catalog",
                skills: [
                  {
                    id: "verification",
                    name: "Behavior checks",
                    description: "Exercise valid and rejected reservations",
                    kind: "recommendation",
                    default: true,
                  },
                ],
              };
        else if (/\/run-creation-requests\/[a-f0-9]{32}$/.test(path)) {
          expect(request.method()).toBe("POST");
          json = await creation.admit(path, request.postDataJSON());
          expect(creation.input).toMatchObject({
            repository_id: repository.id,
            brief: run.brief,
            publish: false,
          });
          admitted = true;
        } else if (path.endsWith("/runs")) {
          expect(request.method()).toBe("GET");
          json = admitted ? [{ ...creation.run, ...run }] : [];
        } else if (path.endsWith(`/runs/${run.id}`))
          json = { ...creation.run, ...run };
        else if (path.endsWith("/plan"))
          json = {
            revision: 1,
            tasks: [
              {
                id: "verify",
                title: "Verify reservation behavior",
                status: "needs_reconciliation",
                attempt: 1,
                instructions: "Inspect retained command evidence.",
                evidence: { checks: run.verification },
              },
            ],
          };
        else if (path.endsWith("/journal"))
          json = {
            valid: true,
            entries: [
              {
                sequence: 1,
                task_id: "verify",
                kind: "intent",
                created_at: "2026-10-13T15:00:00Z",
                digest: "b".repeat(64),
                previous_hash: "0".repeat(64),
                idempotency_key: "fixture-intent",
                payload: {
                  actor_id: "fixture-operator",
                  reason: "unknown effect",
                  argv: ["uv", "run", "pytest"],
                },
              },
            ],
          };
        await route.fulfill({ json });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fixture-bootstrap-token-at-least-24-chars");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await page
        .getByRole("list", { name: "Setup progress" })
        .getByRole("button", { name: /Workspace$/ })
        .click();
      for (const [index, stage] of [
        "workspace",
        "coding-ai",
        "repositories",
        "knowledge",
      ].entries()) {
        await capture(page, `connect-${stage}`, theme, device);
        if (index < 3)
          await page
            .getByRole("button", {
              name: index === 2 ? "Continue to knowledge" : "Continue",
              exact: true,
            })
            .click();
      }
      await navigate(page, "Product definition");
      await page.getByLabel("Customer or user").fill("Warehouse operators");
      await page
        .getByLabel("Problem to solve")
        .fill("Prevent inventory over-reservation");
      await page.getByLabel("Product owners").fill("Inventory team");
      await page.getByLabel("Product owners").blur();
      await capture(page, "product-context", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("Customer journey", { exact: true })
        .fill("Inspect stock and reserve available units");
      await page
        .getByLabel("User stories")
        .fill("As an operator I reserve available stock");
      await capture(page, "product-journeys", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("Acceptance criteria")
        .fill("Over-reservation returns 409");
      await page
        .getByRole("button", { name: "Add feature", exact: true })
        .click();
      await page
        .getByLabel("Feature name", { exact: true })
        .fill("Reserve available stock");
      await page
        .getByLabel("Over-reservation returns 409", { exact: true })
        .check();
      await page
        .getByLabel("Given", { exact: true })
        .fill("Five units are available");
      await page
        .getByLabel("When", { exact: true })
        .fill("The operator requests eight units");
      await page
        .getByLabel("Then", { exact: true })
        .fill("Return 409 and keep the stock unchanged");
      await page
        .getByLabel("Failure and exception paths", { exact: true })
        .fill("Reject invalid quantities without reserving stock");
      await page
        .getByLabel("Feature name", { exact: true })
        .scrollIntoViewIfNeeded();
      await capture(page, "product-feature-mapping", theme, device);
      await page.getByLabel("Given", { exact: true }).scrollIntoViewIfNeeded();
      await capture(page, "product-behavior", theme, device);
      await page.getByLabel("Preferred frontend").selectOption("none");
      await page
        .getByRole("button", { name: "Propose architecture", exact: true })
        .click();
      await expect(
        page.getByText("API-only reservation contract"),
      ).toBeVisible();
      await capture(page, "product-features", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await capture(page, "product-integrations", theme, device);
      await page
        .getByRole("button", { name: "Review definition", exact: true })
        .click();
      await page
        .getByLabel(
          "I reviewed the architecture layers, responsibilities and decisions.",
        )
        .check();
      await page
        .getByLabel(
          "I reviewed the mapped behaviors, failure paths and acceptance links.",
        )
        .check();
      await capture(page, "product-review", theme, device);
      await page
        .getByRole("button", { name: /Save (?:draft|reviewed) definition/ })
        .click();
      await expect(
        page.getByText("Product definition saved.", { exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: /Start run.*revision 1/ }).click();
      await page
        .getByLabel("What should this change accomplish?")
        .fill(run.brief);
      await capture(page, "run-brief", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await capture(page, "run-foundation", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("Commands to run")
        .fill('[["uv", "run", "pytest"]]');
      await capture(page, "run-checks", theme, device);
      await page
        .getByLabel("Independent review provider", { exact: true })
        .scrollIntoViewIfNeeded();
      await capture(page, "run-review-provider", theme, device);
      await page
        .getByLabel("Override project policies for this run", { exact: true })
        .check();
      await page
        .getByLabel("Behavior checks", { exact: false })
        .scrollIntoViewIfNeeded();
      await capture(page, "run-policies", theme, device);
      await page
        .getByLabel("Override project policies for this run", { exact: true })
        .uncheck();
      await page
        .getByRole("button", { name: "Review run", exact: true })
        .click();
      await capture(page, "run-review", theme, device);
      await page
        .getByRole("button", { name: "Start building", exact: true })
        .click();
      await expect.poll(() => admitted).toBe(true);
      expect(creation.run).toMatchObject({ id: run.id, status: "queued" });
      await expect(
        page.getByText("Your run has been opened.", { exact: true }),
      ).toBeVisible();
      await navigate(page, "Factory");
      await page.evaluate(() => document.fonts.ready);
      const floorBounds = await page
        .locator(".factory-live-viewport")
        .evaluate((node) => {
          const scene = node.getBoundingClientRect();
          const main = document.querySelector("main")!.getBoundingClientRect();
          const footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
          return {
            top: scene.top,
            bottom: scene.bottom,
            height: scene.height,
            mainTop: main.top,
            footerTop: footer.top,
          };
        });
      expect(floorBounds.top).toBeGreaterThanOrEqual(floorBounds.mainTop);
      expect(floorBounds.bottom).toBeLessThanOrEqual(floorBounds.footerTop);
      expect(floorBounds.height).toBeGreaterThanOrEqual(180);
      await capture(page, "factory-overview", theme, device);
      await page.getByRole("button", { name: new RegExp(run.brief) }).click();
      await capture(page, "run-context", theme, device);
      for (const tab of ["Changes", "Verification", "Plan", "Artifacts"]) {
        await page.getByRole("tab", { name: tab, exact: true }).click();
        if (tab === "Plan")
          await expect(
            page.getByText("Outcome not recorded", { exact: true }).first(),
          ).toBeVisible();
        if (tab === "Plan")
          await page
            .getByRole("heading", { name: "Execution journal", exact: true })
            .evaluate((element) => element.scrollIntoView({ block: "start" }));
        else
          await page
            .getByRole("tab", { name: tab, exact: true })
            .evaluate((element) => element.scrollIntoView({ block: "start" }));
        await capture(page, `evidence-${tab.toLowerCase()}`, theme, device);
      }
      await expect(
        page.getByText("Your run has been opened.", { exact: true }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Dismiss notification", exact: true })
        .click();
      await navigate(page, "Administration");
      await page
        .locator("main")
        .getByRole("button", { name: "Internet search", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Set up internet search", exact: true })
        .click();
      await expect(
        page.getByLabel("Search provider", { exact: true }),
      ).toHaveValue("tavily");
      await capture(page, "search-provider", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("Provider key", { exact: true })
        .fill("fixture-search-key-not-an-account");
      await page
        .getByLabel("Allowed websites", { exact: true })
        .fill("fireflyframework.org\ngithub.com");
      await capture(page, "search-access", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await capture(page, "search-review", theme, device);
      await page
        .getByRole("button", { name: "Save search settings", exact: true })
        .click();
      await expect(page.getByText("Not tested", { exact: true })).toBeVisible();
      await capture(page, "search-saved", theme, device);
      expect(searchTests).toBe(0);
      await navigate(page, "Administration");
      await page
        .locator("main")
        .getByRole("button", { name: "Learning", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Edit learning settings", exact: true })
        .click();
      await page
        .getByLabel("Read verified learning from flycanon", { exact: true })
        .check();
      await capture(page, "learning-knowledge", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page.getByLabel("Maximum age (hours)", { exact: true }).fill("48");
      await capture(page, "learning-limits", theme, device);
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await capture(page, "learning-review", theme, device);
      await page
        .getByRole("button", { name: "Save learning settings", exact: true })
        .click();
      await expect(page.getByRole("status")).toContainText(
        "Learning settings saved",
      );
      await capture(page, "learning-saved", theme, device, {
        actionSelector: ".learning-actions button.primary",
      });
      await navigate(page, "Knowledge");
      await expect(
        page.locator(
          `#explorer-project option[value="${learningRecord.envelope.context.repository_id}"]`,
        ),
      ).toHaveCount(1);
      await page
        .getByLabel("Knowledge project", { exact: true })
        .selectOption(learningRecord.envelope.context.repository_id);
      await page
        .getByRole("tab", { name: "Learning records", exact: true })
        .click();
      await expect(
        page.getByRole("button", {
          name: learningRecord.envelope.question,
          exact: true,
        }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Refresh records", exact: true })
        .click();
      await expect(page.locator(".learning-records-count")).toContainText(
        "2 of 2 saved records shown",
      );
      await capture(page, "learning-records", theme, device, {
        actionSelector: ".learning-records > header button",
      });
      const selected = page.getByRole("button", {
        name: learningRecord.envelope.question,
        exact: true,
      });
      await selected.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("heading", {
          name: learningRecord.envelope.question,
          exact: true,
        }),
      ).toBeFocused();
      await expect(page.locator(".learning-record-reuse")).toContainText(
        "Saved verification does not establish current eligibility",
      );
      await expect(
        page.getByText("Verified projection", { exact: true }),
      ).toBeVisible();
      await page
        .locator(".learning-record-projection")
        .scrollIntoViewIfNeeded();
      await capture(page, "learning-projection", theme, device);
      await page
        .getByText("Identifiers and verification receipts", { exact: true })
        .click();
      await page
        .getByRole("heading", {
          name: "Native verification receipts",
          exact: true,
        })
        .scrollIntoViewIfNeeded();
      await expect(
        page.getByText("Journal sequence 6", { exact: true }),
      ).toBeVisible();
      if (device === "desktop") {
        const main = await page.locator(".workspace-main > main").boundingBox(),
          choice = await selected.boundingBox();
        expect(main).not.toBeNull();
        expect(choice).not.toBeNull();
        expect(choice!.y).toBeGreaterThanOrEqual(main!.y);
        expect(choice!.y + choice!.height).toBeLessThanOrEqual(
          main!.y + main!.height,
        );
        expect(
          await selected.evaluate((button) => {
            const box = button.getBoundingClientRect();
            return (
              document
                .elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
                ?.closest("button") === button
            );
          }),
        ).toBe(true);
      }
      await capture(page, "learning-provenance", theme, device);
      await page
        .getByRole("button", {
          name: failureCandidate.envelope.question,
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("heading", {
          name: failureCandidate.envelope.question,
          exact: true,
        }),
      ).toBeFocused();
      await expect(page.locator(".learning-record-reuse")).toContainText(
        "It cannot authorize reuse of a verified local outcome",
      );
      await expect(page.locator(".learning-record-projection")).toContainText(
        "Candidate observations do not have a verified local-outcome projection",
      );
      await expect(
        page.getByText("Verified projection", { exact: true }),
      ).toHaveCount(0);
      await capture(page, "learning-candidate", theme, device);
      expect(learningReads.every((call) => call.startsWith("GET "))).toBe(true);
      expect(
        learningReads.some((call) => call.includes(failureCandidate.id)),
      ).toBe(false);

      await selected.click();
      await page.locator(".learning-records-filter-toggle > summary").click();
      await page
        .getByLabel("Saved state", { exact: true })
        .selectOption("verified");
      await page
        .getByRole("button", { name: "Invalidate evidence", exact: true })
        .click();
      const reason = "Inventory dependency changed.";
      await page
        .getByLabel("Reason for invalidation", { exact: true })
        .fill(reason);
      const dialogHeading = page.getByRole("dialog").getByRole("heading", {
        name: "Invalidate saved evidence",
        exact: true,
      });
      const assertHeadingHit = async (selector: string) => {
        await expect
          .poll(() =>
            page.locator(selector).evaluate((element) => {
              const box = element.getBoundingClientRect();
              return (
                box.top >= 0 &&
                document.elementFromPoint(
                  box.x + box.width / 2,
                  box.y + box.height / 2,
                ) === element
              );
            }),
          )
          .toBe(true);
      };
      await expect(dialogHeading).toBeVisible();
      await assertHeadingHit(
        ".learning-invalidation-dialog .access-dialog-heading h2",
      );
      const assertLifecycle = async (completeReceipt = false) => {
        await expect
          .poll(() =>
            page
              .locator(".learning-record-invalidation > h4")
              .evaluate((heading, complete) => {
                const box = heading.getBoundingClientRect();
                const shell = document
                  .querySelector(".workspace-main > header")!
                  .getBoundingClientRect();
                const footer = document
                  .querySelector(".workspace-main > footer")!
                  .getBoundingClientRect();
                const result = document
                  .querySelector(".learning-invalidation-result")!
                  .getBoundingClientRect();
                return (
                  box.top >= shell.bottom + 16 &&
                  document.elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  ) === heading &&
                  (!complete || result.bottom <= footer.top)
                );
              }, completeReceipt),
          )
          .toBe(true);
      };
      const alignLifecycle = async (completeReceipt = false) => {
        await page
          .locator(".learning-record-invalidation > h4")
          .evaluate((heading) => {
            const shell = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect();
            const main = heading.closest("main")!;
            main.scrollTop +=
              heading.getBoundingClientRect().top - shell.bottom - 20;
          });
        await assertLifecycle(completeReceipt);
      };
      await capture(page, "learning-invalidation-reason", theme, device);
      await assertHeadingHit(
        ".learning-invalidation-dialog .access-dialog-heading h2",
      );
      await page
        .getByRole("button", { name: "Review invalidation", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "Confirm invalidation", exact: true }),
      ).toBeDisabled();
      await page
        .getByLabel(
          "I understand this excludes the evidence from reuse and keeps its history.",
          { exact: true },
        )
        .check();
      for (const value of [
        new URL("/api/v1", page.url()).href,
        learningRepository.id,
        learningRecord.id,
        "f".repeat(32),
      ])
        await expect(
          page
            .getByRole("dialog", {
              name: "Invalidate saved evidence",
              exact: true,
            })
            .getByText(value, { exact: true }),
        ).toBeVisible();
      await page.locator(".learning-invalidation-ack").scrollIntoViewIfNeeded();
      await page
        .locator(".learning-invalidation-dialog .wizard-field-scroll")
        .evaluate((field) => {
          const viewport = field.getBoundingClientRect();
          const reason = field
            .querySelector(".learning-invalidation-reason")!
            .getBoundingClientRect();
          const acknowledgment = field
            .querySelector(".learning-invalidation-ack")!
            .getBoundingClientRect();
          const margin = Math.max(
            0,
            (viewport.height - (acknowledgment.bottom - reason.top)) / 2,
          );
          field.scrollTop += reason.top - viewport.top - margin;
        });
      const readReviewBounds = () =>
        page.locator(".learning-invalidation-dialog").evaluate((dialog) => {
          const field = dialog
            .querySelector(".wizard-field-scroll")!
            .getBoundingClientRect();
          return [
            ".learning-invalidation-reason",
            ".learning-invalidation-ack",
          ].map((selector) => {
            const box = dialog.querySelector(selector)!.getBoundingClientRect();
            return {
              selector,
              top: box.top,
              bottom: box.bottom,
              fieldTop: field.top,
              fieldBottom: field.bottom,
              inside: box.top >= field.top && box.bottom <= field.bottom,
            };
          });
        });
      await expect
        .poll(async () => (await readReviewBounds()).every((row) => row.inside))
        .toBe(true);
      await capture(page, "learning-invalidation-review", theme, device, {
        resetFields: false,
      });
      await expect
        .poll(async () => (await readReviewBounds()).every((row) => row.inside))
        .toBe(true);
      await assertHeadingHit(
        ".learning-invalidation-dialog .access-dialog-heading h2",
      );
      await page
        .getByRole("button", { name: "Confirm invalidation", exact: true })
        .click();
      await expect(
        page.getByText("Original request unresolved", { exact: true }),
      ).toBeVisible();
      expect(invalidationWrites).toBe(1);
      expect(invalidationReads).toHaveLength(0);
      const original = invalidation!;
      expect(original.reason).toBe(reason);
      expect(original.request_id).toMatch(/^[a-f0-9]{32}$/);
      await navigate(page, "Factory");
      await navigate(page, "Knowledge");
      await page
        .getByLabel("Knowledge project", { exact: true })
        .selectOption(learningRepository.id);
      await page
        .getByRole("tab", { name: "Learning records", exact: true })
        .click();
      await selected.click();
      await page.locator(".learning-records-filter-toggle > summary").click();
      await page
        .getByLabel("Saved state", { exact: true })
        .selectOption("verified");
      await page
        .getByRole("button", { name: "Check original request", exact: true })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "The exact request has not been observed",
      );
      await expect(page.getByText(reason, { exact: true })).toBeVisible();
      await expect(
        page.getByText(original.request_id, { exact: true }),
      ).toBeVisible();
      expect(invalidationWrites).toBe(1);
      expect(invalidationReads).toHaveLength(1);
      await alignLifecycle();
      await capture(page, "learning-invalidation-unresolved", theme, device, {
        actionSelector: ".learning-record-invalidation button.secondary",
      });
      await assertLifecycle();
      receiptReady = true;
      await page
        .getByRole("button", { name: "Check original request", exact: true })
        .click();
      const confirmed = page.getByText("Original invalidation confirmed", {
        exact: true,
      });
      await expect(confirmed).toBeVisible();
      await expect(
        page.getByText("0 of 2 saved records shown", { exact: true }),
      ).toBeVisible();
      await expect(selected).toHaveCount(0);
      await expect(
        page.locator(".learning-record-filter-context"),
      ).toContainText("The selected record no longer matches this filter");
      await expect(page.getByText(reason, { exact: true })).toBeVisible();
      for (const value of [
        original.request_id,
        "f".repeat(32),
        "2".repeat(32),
        "3".repeat(64),
      ])
        await expect(
          page
            .locator(".learning-invalidation-result")
            .getByText(value, { exact: true }),
        ).toBeVisible();
      await expect(page.locator(".learning-invalidation-result")).toContainText(
        "Historical event: invalidated. Current record state: invalidated.",
      );
      expect(invalidationWrites).toBe(1);
      expect(invalidationReads).toEqual([
        `/api/v1/learning/records/${learningRecord.id}/invalidations/${original.request_id}`,
        `/api/v1/learning/records/${learningRecord.id}/invalidations/${original.request_id}`,
      ]);
      expect(invalidation).toEqual(original);
      await alignLifecycle(device === "desktop");
      await expect(confirmed).toBeInViewport();
      await capture(page, "learning-invalidation-confirmed", theme, device);
      await assertLifecycle(device === "desktop");
    });
  }

for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"]) {
    test(`guided fixture journey existing source ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const source = "fixture-reservation-source";
      const projection = {
        id: "1".repeat(32),
        record_id: learningRecord.id,
        state: "source_admitted",
        request_id: "2".repeat(32),
        workspace_id: "fixture-learning-inventory",
        source_id: source,
        source_status: "queued",
        content_sha256: "3".repeat(64),
        payload_digest: "4".repeat(64),
        target: { revision: "5".repeat(32) },
        citations: [],
      };
      const eventRequest = "8".repeat(32);
      function canonical(value: unknown): string {
        if (value === null || typeof value !== "object")
          return JSON.stringify(value);
        if (Array.isArray(value))
          return "[" + value.map(canonical).join(",") + "]";
        const row = value as Record<string, unknown>;
        return (
          "{" +
          Object.keys(row)
            .sort()
            .map((key) => JSON.stringify(key) + ":" + canonical(row[key]))
            .join(",") +
          "}"
        );
      }
      const hash = (value: unknown) =>
        createHash("sha256").update(canonical(value)).digest("hex");
      let observation: Record<string, string> | null = null;
      let writes = 0;
      let receiptReady = false;
      const reads: string[] = [];
      const attemptReads: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (
          path.endsWith(
            `/learning/records/${learningRecord.id}/projection/observation-attempts`,
          )
        ) {
          expect(request.method()).toBe("POST");
          writes++;
          expect(writes).toBe(1);
          observation = request.postDataJSON();
          expect(observation!.source_id).toBe(source);
          expect(observation!.attempt_id).toMatch(/^obs2_[a-f0-9]{32}$/);
          const marker = await page.evaluate(() => {
            const keys = Object.keys(sessionStorage).filter((key) =>
              key.startsWith("forge.learning-observation.v2:"),
            );
            if (keys.length !== 1)
              throw new Error(
                "Original source request must be retained before dispatch",
              );
            return JSON.parse(sessionStorage.getItem(keys[0])!);
          });
          expect(marker).toMatchObject({
            base: new URL("/api/v1", page.url()).href,
            actor: "f".repeat(32),
            repository: learningRepository.id,
            project: learningRecord.envelope.context.project_id,
            run: learningRecord.envelope.run_id,
            record: learningRecord.id,
            requestId: observation!.attempt_id.slice(5),
            version: 2,
            sourceDigest: hash({ source_id: source }),
            inputDigest: hash(observation),
            projection: projection.id,
            submissionRequest: projection.request_id,
            workspace: projection.workspace_id,
            targetRevision: projection.target.revision,
            payloadDigest: projection.payload_digest,
            contentHash: projection.content_sha256,
          });
          expect(marker).not.toHaveProperty("source");
          return route.abort("failed");
        }
        if (
          path.includes(
            `/learning/records/${learningRecord.id}/projection/observation-attempts/`,
          )
        ) {
          expect(request.method()).toBe("GET");
          expect(observation).not.toBeNull();
          expect(path).toBe(
            `/api/v1/learning/records/${learningRecord.id}/projection/observation-attempts/${observation!.attempt_id}`,
          );
          attemptReads.push(path);
          if (!receiptReady)
            return route.fulfill({
              status: 404,
              json: { detail: "Exact attempt not observed" },
            });
          const packet = {
            version: 2,
            ...observation!,
            record_id: learningRecord.id,
            repository_id: learningRepository.id,
            project_id: learningRecord.envelope.context.project_id,
            run_id: learningRecord.envelope.run_id,
            actor_id: "f".repeat(32),
            input_digest: hash(observation),
            source_digest: hash({ source_id: source }),
            state: "verified",
            disposition: "verified",
            created_at: "2026-10-15T01:39:00Z",
            finished_at: "2026-10-15T01:40:00Z",
            observation: {
              event_id: "6".repeat(32),
              request_id: eventRequest,
              digest: "7".repeat(64),
            },
          };
          Reflect.deleteProperty(packet, "source_id");
          return route.fulfill({ json: { ...packet, digest: hash(packet) } });
        }
        if (
          path.includes(
            `/learning/records/${learningRecord.id}/projection/observations/`,
          )
        ) {
          expect(request.method()).toBe("GET");
          expect(observation).not.toBeNull();
          expect(path).toBe(
            `/api/v1/learning/records/${learningRecord.id}/projection/observations/${eventRequest}`,
          );
          reads.push(path);
          if (!receiptReady)
            return route.fulfill({
              status: 404,
              json: {
                detail: "Learning source observation request not observed",
              },
            });
          return route.fulfill({
            json: {
              event_id: "6".repeat(32),
              record_id: learningRecord.id,
              repository_id: learningRepository.id,
              run_id: learningRecord.envelope.run_id,
              request_id: eventRequest,
              kind: "projection_observation",
              event_state: "verified",
              current_record_state: "invalidated",
              current_projection_state: "verified",
              actor_id: "f".repeat(32),
              projection_id: projection.id,
              submission_request_id: projection.request_id,
              workspace_id: projection.workspace_id,
              source_id: source,
              target_revision: projection.target.revision,
              payload_digest: projection.payload_digest,
              content_sha256: projection.content_sha256,
              citations: [
                { source_id: source, chunk_id: "fixture-reservation-chunk:1" },
              ],
              created_at: "2026-10-15T01:40:00Z",
              sequence: 7,
              digest: "7".repeat(64),
            },
          });
        }
        expect(request.method()).toBe("GET");
        let json: unknown = {};
        if (path.endsWith("/repositories")) json = [learningRepository];
        else if (path.endsWith(`/${learningRepository.id}/learning`))
          json = [
            {
              ...learningRecord,
              state: receiptReady ? "invalidated" : "verified",
            },
          ];
        else if (
          path.endsWith(`/learning/records/${learningRecord.id}/projection`)
        )
          json = {
            ...projection,
            state: receiptReady ? "verified" : "source_admitted",
            source_status: receiptReady ? "ingested" : "queued",
            citations: receiptReady
              ? [{ source_id: source, chunk_id: "fixture-reservation-chunk:1" }]
              : [],
          };
        else if (
          [
            "/runs",
            "/projects",
            "/providers",
            "/deployment-targets",
            "/assistant/conversations",
          ].some((suffix) => path.endsWith(suffix))
        )
          json = [];
        else if (path.endsWith("/knowledge/explore"))
          json = {
            workspace_id: "fixture-learning-inventory",
            scope: "project",
            sources: [],
            knowledge: [],
            offset: 0,
            limit: 20,
            totals: { sources: 0, knowledge: 0 },
            browsing_available: true,
            warnings: [],
          };
        await route.fulfill({ json });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fixture-bootstrap-token-at-least-24-chars");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      const selectRecord = async () => {
        await navigate(page, "Knowledge");
        await page
          .getByLabel("Knowledge project", { exact: true })
          .selectOption(learningRepository.id);
        await page
          .getByRole("tab", { name: "Learning records", exact: true })
          .click();
        await page
          .getByRole("button", {
            name: learningRecord.envelope.question,
            exact: true,
          })
          .click();
      };
      await selectRecord();
      await page.locator(".learning-records-filter-toggle > summary").click();
      await page
        .getByLabel("Saved state", { exact: true })
        .selectOption("verified");
      await page
        .getByRole("button", { name: "Verify existing source", exact: true })
        .click();
      await expect(
        page.getByLabel("Existing source identifier", { exact: true }),
      ).toHaveValue(source);
      const assertDialogHeading = async () => {
        await expect
          .poll(() =>
            page
              .locator(".learning-observation-dialog .access-dialog-heading h2")
              .evaluate((element) => {
                const box = element.getBoundingClientRect();
                return (
                  box.top >= 0 &&
                  document.elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  ) === element
                );
              }),
          )
          .toBe(true);
      };
      await capture(page, "learning-observation-source", theme, device);
      await assertDialogHeading();
      await page
        .getByRole("button", { name: "Review source", exact: true })
        .click();
      const confirmation = page.getByRole("button", {
        name: "Verify source",
        exact: true,
      });
      await expect(confirmation).toBeDisabled();
      const details = page.getByRole("dialog").locator("details");
      for (const value of [
        new URL("/api/v1", page.url()).href,
        "f".repeat(32),
        learningRepository.id,
        learningRecord.id,
        projection.workspace_id,
        projection.target.revision,
      ])
        await expect(details).toContainText(value);
      const acknowledgment = page.getByLabel(
        "I understand this reads the existing source and records a local verification; it does not submit or ingest a source.",
        { exact: true },
      );
      await acknowledgment.check();
      await acknowledgment.scrollIntoViewIfNeeded();
      const assertAcknowledgment = async () => {
        await expect
          .poll(() =>
            acknowledgment.evaluate((element) => {
              const box = element.getBoundingClientRect(),
                field = element
                  .closest(".wizard-field-scroll")!
                  .getBoundingClientRect();
              return box.top >= field.top && box.bottom <= field.bottom;
            }),
          )
          .toBe(true);
      };
      await assertAcknowledgment();
      await capture(page, "learning-observation-review", theme, device, {
        resetFields: false,
      });
      await assertAcknowledgment();
      await assertDialogHeading();
      await confirmation.click();
      await expect(page.getByRole("alert")).toContainText(
        "The original observation could not be confirmed",
      );
      const original = { ...observation! };
      expect(writes).toBe(1);
      expect(reads).toHaveLength(0);
      await navigate(page, "Factory");
      await selectRecord();
      await page.locator(".learning-records-filter-toggle > summary").click();
      await page
        .getByLabel("Saved state", { exact: true })
        .selectOption("verified");
      await page
        .getByRole("button", {
          name: "Check original verification",
          exact: true,
        })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "The exact observation has not been observed",
      );
      expect(writes).toBe(1);
      expect(attemptReads).toHaveLength(1);
      expect(reads).toHaveLength(0);
      const alignObservation = async (complete = false) => {
        await page
          .locator(".learning-record-observation > h4")
          .evaluate((heading) => {
            const shell = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect();
            const main = heading.closest("main")!;
            main.scrollTop +=
              heading.getBoundingClientRect().top - shell.bottom - 20;
          });
        await assertObservation(complete);
      };
      const assertObservation = async (complete = false) => {
        await expect
          .poll(() =>
            page
              .locator(".learning-record-observation > h4")
              .evaluate((heading, complete) => {
                const box = heading.getBoundingClientRect(),
                  shell = document
                    .querySelector(".workspace-main > header")!
                    .getBoundingClientRect(),
                  footer = document
                    .querySelector(".workspace-main > footer")!
                    .getBoundingClientRect();
                const result = document
                  .querySelector(".learning-observation-result")!
                  .getBoundingClientRect();
                return (
                  box.top >= shell.bottom + 16 &&
                  document.elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  ) === heading &&
                  (!complete || result.bottom <= footer.top)
                );
              }, complete),
          )
          .toBe(true);
      };
      const retained = page.locator(".learning-observation-result");
      await expect(retained).not.toContainText(source);
      await expect(retained).toContainText(
        "Unconfirmed source text is not saved",
      );
      await expect(retained).toContainText(original.attempt_id);
      await alignObservation();
      await capture(page, "learning-observation-unresolved", theme, device, {
        actionSelector: ".learning-record-observation button.secondary",
      });
      await assertObservation();
      receiptReady = true;
      await page
        .getByRole("button", {
          name: "Check original verification",
          exact: true,
        })
        .click();
      await expect(
        page.getByText("Original source verification confirmed", {
          exact: true,
        }),
      ).toBeVisible();
      const projectionContractError = page.getByText(
        "Learning records could not be read. The response does not match this repository's evidence contract.",
        { exact: true },
      );
      const verifiedProjection = page
        .locator(".learning-record-projection")
        .getByText("Verified projection", { exact: true });
      await expect
        .poll(
          async () =>
            (await projectionContractError.isVisible()) ||
            (await verifiedProjection.isVisible()),
        )
        .toBe(true);
      await expect(projectionContractError).toHaveCount(0);
      await expect(verifiedProjection).toBeVisible();
      await expect(retained).toContainText(
        "Historical observation: verified. Current record state: invalidated. Current projection state: verified.",
      );
      await expect(page.locator(".learning-records-count")).toHaveText(
        "0 of 1 saved records shown",
      );
      await expect(
        page.locator(".learning-record-filter-context"),
      ).toContainText("The selected record no longer matches this filter");
      for (const value of [
        original.attempt_id,
        "f".repeat(32),
        "6".repeat(32),
        "7".repeat(64),
      ])
        await expect(retained).toContainText(value);
      expect(writes).toBe(1);
      expect(attemptReads).toEqual([
        `/api/v1/learning/records/${learningRecord.id}/projection/observation-attempts/${original.attempt_id}`,
        `/api/v1/learning/records/${learningRecord.id}/projection/observation-attempts/${original.attempt_id}`,
      ]);
      expect(reads).toEqual([
        `/api/v1/learning/records/${learningRecord.id}/projection/observations/${eventRequest}`,
      ]);
      expect(observation).toEqual(original);
      await alignObservation(device === "desktop");
      await capture(page, "learning-observation-confirmed", theme, device);
      await assertObservation(device === "desktop");
    });
  }

// Controlled guide snapshots qualify presentation, not live campaign or runner activity.
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"]) {
    test(`guided fixture journey continuation and campaign ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const report = structuredClone(verifiedReport);
      report.campaign.recorded_status = "succeeded";
      function canonical(value: unknown): string {
        if (value === null || typeof value !== "object")
          return JSON.stringify(value);
        if (Array.isArray(value))
          return "[" + value.map(canonical).join(",") + "]";
        const row = value as Record<string, unknown>;
        return (
          "{" +
          Object.keys(row)
            .sort()
            .map((key) => JSON.stringify(key) + ":" + canonical(row[key]))
            .join(",") +
          "}"
        );
      }
      report.digest = createHash("sha256")
        .update(
          canonical(
            Object.fromEntries(
              Object.entries(report).filter(([key]) => key !== "digest"),
            ),
          ),
        )
        .digest("hex");
      const member = report.components[0];
      const guideRepository = {
        id: member.repository_id,
        name: "Fixture Campaign Inventory",
        provider: "git",
        url: "/local/fixture-campaign-inventory",
        default_branch: "main",
      };
      const component = {
        id: member.component_id,
        name: "Inventory API",
        repository_id: guideRepository.id,
        path: ".",
        responsibility: "Fictional inventory checks",
        kind: "api",
        backend: "pyfly",
        frontend: "none",
        verification: [],
        depends_on: [],
      };
      const product = {
        id: report.project_id,
        revision: 1,
        digest: report.campaign.project_digest,
        created_at: "2026-10-15T12:00:00Z",
        updated_at: "2026-10-15T12:00:00Z",
        definition: {
          name: "Fixture inventory campaign",
          description: "Controlled guide for saved campaign evidence",
          owners: ["Guide fixture"],
          topology: "monorepo",
          product_repository_id: guideRepository.id,
          components: [component],
        },
      };
      const queued = {
        id: "6".repeat(32),
        repository_id: guideRepository.id,
        project_id: null,
        brief: "Fixture original queued command",
        status: "running",
        agent: "agentic",
        publish: false,
      };
      const commands = [
        {
          execution_id: "4".repeat(32),
          task_id: "verify",
          attempt: 1,
          command_label: "Python command",
          argument_count: 2,
          source_digest: "a".repeat(64),
          invocation_digest: "b".repeat(64),
          wal_digest: "c".repeat(64),
          binding_digest: "d".repeat(64),
        },
        {
          execution_id: "4".repeat(12) + "5".repeat(20),
          task_id: "verify",
          attempt: 1,
          command_label: "Python command",
          argument_count: 3,
          source_digest: "a".repeat(64),
          invocation_digest: "e".repeat(64),
          wal_digest: "f".repeat(64),
          binding_digest: "1".repeat(64),
        },
      ];
      const calls: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        calls.push(request.method() + " " + path);
        expect(request.method()).toBe("GET");
        const json = path.endsWith("/runner-continuations/candidates")
          ? {
              version: 1,
              run_id: queued.id,
              repository_id: guideRepository.id,
              project_id: null,
              actor_id: "f".repeat(32),
              state: "requires_leader_confirmation",
              reason:
                "Controlled guide: leader confirmation is still required.",
              commands,
            }
          : path.endsWith(`/campaigns/${report.campaign.id}/report`)
            ? report
            : path.endsWith("/campaigns")
              ? [
                  {
                    id: report.campaign.id,
                    project_id: product.id,
                    status: "succeeded",
                    created_at: "2026-10-15T12:00:00Z",
                  },
                ]
              : path.endsWith("/repositories")
                ? [guideRepository]
                : path.endsWith("/projects")
                  ? [product]
                  : path.endsWith("/projects/" + product.id)
                    ? {
                        ...product,
                        components: [
                          {
                            ...component,
                            latest_status: "succeeded",
                            repository_run_count: 1,
                            runs: [
                              {
                                id: member.run_id,
                                status: "succeeded",
                                branch: member.branch,
                                created_at: "2026-10-15T12:00:00Z",
                              },
                            ],
                          },
                        ],
                        counts: { succeeded: 1 },
                      }
                    : path.endsWith("/runs")
                      ? [queued]
                      : path.endsWith("/runs/" + queued.id)
                        ? queued
                        : path.endsWith("/plan")
                          ? { revision: 1, tasks: [] }
                          : path.endsWith("/journal")
                            ? { valid: true, entries: [] }
                            : path.endsWith("/health")
                              ? { status: "ok", execution_ready: false }
                              : [];
        await route.fulfill({ json });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fixture-bootstrap-token-at-least-24-chars");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "API connected", exact: true }),
      ).toBeVisible();
      await navigate(page, "Factory");
      await page
        .getByRole("button", { name: /Fixture original queued command/ })
        .click();
      await page.getByRole("tab", { name: "Plan", exact: true }).click();
      await page
        .getByRole("button", { name: "Request continuation", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Request original command continuation",
        exact: true,
      });
      const first = dialog.getByRole("radio", {
        name: new RegExp("Command 1.*" + commands[0].execution_id),
      });
      await first.check();
      await expect(dialog.getByRole("radio")).toHaveCount(2);
      await capture(page, "runner-continuation-selection", theme, device);
      await dialog
        .getByRole("button", { name: "Review original command", exact: true })
        .click();
      await dialog
        .getByLabel(
          "I understand the leader must confirm this original command.",
          { exact: true },
        )
        .check();
      await expect(
        dialog.getByRole("button", {
          name: "Send continuation request",
          exact: true,
        }),
      ).toBeEnabled();
      await capture(page, "runner-continuation-review", theme, device);
      await dialog
        .getByRole("button", { name: "Cancel review", exact: true })
        .click();
      expect(
        await page.evaluate(() =>
          Object.keys(sessionStorage).filter((key) =>
            key.startsWith("forge.runner-continuation.v1:"),
          ),
        ),
      ).toHaveLength(0);
      await navigate(page, "Product projects");
      await expect(
        page.getByRole("heading", {
          name: product.definition.name,
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page
          .getByRole("region", {
            name: "Product component topology",
            exact: true,
          })
          .getByText("succeeded", { exact: true }),
      ).toBeVisible();
      expect(calls.some((call) => call.includes("/campaigns"))).toBe(false);
      await page
        .getByRole("button", { name: "Load campaigns", exact: true })
        .click();
      await page
        .getByLabel("Campaign", { exact: true })
        .selectOption(report.campaign.id);
      expect(calls.some((call) => call.endsWith("/report"))).toBe(false);
      await page
        .getByRole("button", { name: "View report", exact: true })
        .click();
      const snapshot = page.getByRole("region", {
        name: "Campaign report snapshot",
        exact: true,
      });
      const align = async (selector: string) => {
        await page.locator(selector).evaluate((element) => {
          const main = element.closest("main")!,
            shell = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect();
          main.scrollTop +=
            element.getBoundingClientRect().top - shell.bottom - 16;
        });
        await expect
          .poll(() =>
            page.locator(selector).evaluate((element) => {
              const box = element.getBoundingClientRect(),
                shell = document
                  .querySelector(".workspace-main > header")!
                  .getBoundingClientRect();
              return (
                box.top >= shell.bottom &&
                element.contains(
                  document.elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  ),
                )
              );
            }),
          )
          .toBe(true);
      };
      await expect(
        snapshot.getByText("Succeeded", { exact: true }).first(),
      ).toBeVisible();
      await expect(
        snapshot.getByText("Verified", { exact: true }).first(),
      ).toBeVisible();
      await align(".campaign-report-snapshot > h3");
      await capture(page, "campaign-report-summary", theme, device, {
        actionSelector: ".campaign-report-components button",
      });
      await snapshot
        .getByRole("button", { name: "Inspect component api", exact: true })
        .click();
      await snapshot
        .getByRole("tab", { name: "References", exact: true })
        .click();
      await align(".campaign-report-detail > h4");
      await expect(
        snapshot.getByRole("heading", {
          name: "Component journal references",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        snapshot.getByText(member.run_id, { exact: true }),
      ).toBeVisible();
      await capture(page, "campaign-report-references", theme, device, {
        actionSelector: '.campaign-report-tabs [aria-selected="true"]',
      });
      expect(calls.filter((call) => call.endsWith("/campaigns"))).toHaveLength(
        1,
      );
      expect(calls.filter((call) => call.endsWith("/report"))).toHaveLength(1);
      expect(calls.every((call) => call.startsWith("GET "))).toBe(true);
    });
  }

// Controlled source declarations illustrate inspection; no Git checkout, application or model runs.
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`guided fixture journey source analysis ${device} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const sourceRepository = { ...repository, id: "b".repeat(32) };
      const project = {
        id: "c".repeat(32),
        definition: {
          name: "Inventory product",
          description:
            "Existing inventory API and customer portal in one repository",
          owners: ["Inventory team"],
          topology: "monorepo",
          components: [
            {
              id: "api",
              name: "Inventory API",
              repository_id: sourceRepository.id,
              path: "apps/api",
              kind: "api",
              backend: "pyfly",
              frontend: "none",
              verification: [],
              depends_on: [],
              responsibility: "Stock reservations",
            },
            {
              id: "portal",
              name: "Customer portal",
              repository_id: sourceRepository.id,
              path: "apps/portal",
              kind: "web",
              backend: "none",
              frontend: "react",
              verification: [],
              depends_on: ["api"],
              responsibility: "Customer stock view",
            },
          ],
        },
        revision: 1,
        digest: "d".repeat(64),
        updated_at: "2026-10-15T12:00:00Z",
        created_at: "2026-10-15T12:00:00Z",
      };
      const commit = "f".repeat(40);
      let report: Record<string, unknown> | null = null;
      const inspections: unknown[] = [],
        reviews: unknown[] = [];
      const declarations = [
        {
          kind: "http_route",
          value: "/stock",
          path: "apps/api/app.py",
          line: 12,
          excerpt: '@get_mapping("/stock")',
        },
        {
          kind: "declared_dependency",
          value: '"react": "^19.0.0"',
          path: "apps/portal/package.json",
          line: 8,
          excerpt: '"react": "^19.0.0"',
        },
      ];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        let json: unknown = {};
        if (path.endsWith("/repositories")) json = [sourceRepository];
        if (
          ["/runs", "/providers", "/deployment-targets"].some((suffix) =>
            path.endsWith(suffix),
          )
        )
          json = [];
        if (path.endsWith("/projects")) json = [project];
        if (path.endsWith(`/projects/${project.id}`))
          json = {
            ...project,
            components: project.definition.components.map((component) => ({
              ...component,
              latest_status: "not_started",
              repository_run_count: 0,
              runs: [],
            })),
            counts: { not_started: 2 },
          };
        if (path.endsWith("/analyses")) {
          if (request.method() === "POST") {
            inspections.push(request.postDataJSON());
            expect(inspections).toEqual([{ component_ids: ["api", "portal"] }]);
            report = {
              id: "a".repeat(32),
              project_id: project.id,
              status: "ready_for_review",
              reviewed: false,
              method: "source_inspection",
              digest: "e".repeat(64),
              created_at: project.created_at,
              components: project.definition.components.map(
                (component, index) => ({
                  ...component,
                  commit,
                  execution: "source_only",
                  skipped_files: 2,
                  facts: [
                    {
                      kind: declarations[index].kind,
                      value: declarations[index].value,
                      confidence: "observed",
                      source: {
                        path: declarations[index].path,
                        line: declarations[index].line,
                        excerpt: declarations[index].excerpt,
                        blob: String(index).repeat(40),
                        commit,
                      },
                    },
                  ],
                }),
              ),
              documents: {
                "functional-as-is.md":
                  "# Observed source declarations\n\nTwo component roots share one source commit. Runtime behavior remains unverified.",
              },
              open_questions: [
                "Which customer roles may reserve stock?",
                "Which checks establish the existing application's runtime behavior?",
              ],
              functional_definition: null,
            };
            json = report;
          } else json = report ? [report] : [];
        }
        if (path.endsWith(`/analyses/${"a".repeat(32)}`)) json = report;
        if (path.endsWith(`/analyses/${"a".repeat(32)}/review`)) {
          reviews.push(request.postDataJSON());
          expect(reviews).toEqual([{ digest: "e".repeat(64), reviewed: true }]);
          report = { ...report, status: "reviewed", reviewed: true };
          json = report;
        }
        await route.fulfill({ json });
      });
      await page.goto("/");
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("test-operator-token-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Product projects");
      await expect(page.locator(".project-overview-header")).toContainText(
        "monorepo · revision",
      );
      await page
        .getByRole("button", { name: "Open source inspection", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Source inspection review",
        exact: true,
      });
      const shot = async (stage: string) => {
        await capture(page, stage, theme, device, {
          resetFields: stage !== "source-analysis-review",
        });
        await expect(dialog.locator(".wizard-heading")).toBeInViewport();
        const controlsFit = await dialog.evaluate((element) => {
          const box = element.getBoundingClientRect();
          return [...element.querySelectorAll(".wizard-actions button")].every(
            (button) => {
              const control = button.getBoundingClientRect();
              return (
                control.top >= box.top &&
                control.bottom <= Math.min(box.bottom, innerHeight) &&
                button.contains(
                  document.elementFromPoint(
                    control.x + control.width / 2,
                    control.y + control.height / 2,
                  ),
                )
              );
            },
          );
        });
        expect(controlsFit).toBe(true);
        const path = testInfo.outputPath(`${stage}.png`);
        await page.screenshot({ path });
        await testInfo.attach(stage, {
          path,
          contentType: "image/png",
        });
      };
      await expect(dialog.getByRole("checkbox")).toHaveCount(2);
      for (const name of [
        "Inventory API · apps/api",
        "Customer portal · apps/portal",
      ])
        await expect(
          dialog.getByRole("checkbox", { name, exact: true }),
        ).toBeChecked();
      await shot("source-analysis-scope");
      expect(inspections).toHaveLength(0);
      await dialog
        .getByRole("button", { name: "Inspect configured source", exact: true })
        .click();
      await expect(
        dialog.getByText("2 inspected components · 2 observed declarations", {
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        dialog.getByText("ready for review · source inspection", {
          exact: true,
        }),
      ).toBeVisible();
      await shot("source-analysis-receipt");
      await dialog
        .getByRole("button", { name: "Continue", exact: true })
        .click();
      for (const declaration of declarations) {
        await expect(
          dialog
            .locator(".source-fact > p")
            .filter({ hasText: declaration.value }),
        ).toHaveText(declaration.value);
        await expect(
          dialog.getByText(`${declaration.path}:${declaration.line}`, {
            exact: true,
          }),
        ).toBeAttached();
      }
      await expect(
        dialog
          .locator(".source-evidence-component > p code")
          .filter({ hasText: commit }),
      ).toHaveCount(2);
      await expect(
        dialog.getByText(
          "1 observed declaration · 2 skipped files · source only",
          { exact: true },
        ),
      ).toHaveCount(2);
      await expect(dialog.getByText("apps/api", { exact: true })).toBeVisible();
      await expect(
        dialog.getByText("apps/portal", { exact: true }),
      ).toBeAttached();
      await shot("source-analysis-evidence");
      await dialog
        .getByRole("button", { name: "Continue", exact: true })
        .click();
      const acknowledge = dialog.getByRole("button", {
        name: "Acknowledge this report digest",
        exact: true,
      });
      await expect(acknowledge).toBeDisabled();
      await expect(
        dialog.getByText("Which customer roles may reserve stock?", {
          exact: true,
        }),
      ).toBeVisible();
      await dialog
        .getByRole("checkbox", {
          name: "I reviewed the source evidence and open questions. This does not claim observed runtime behavior.",
          exact: true,
        })
        .check();
      await expect(acknowledge).toBeEnabled();
      await dialog.getByRole("checkbox").evaluate((input) => {
        input.closest("label")!.scrollIntoView({ block: "center" });
      });
      const acknowledgementFits = async () =>
        dialog.getByRole("checkbox").evaluate((input) => {
          const label = input.closest("label")!,
            box = label.getBoundingClientRect(),
            field = label
              .closest(".wizard-field-scroll")!
              .getBoundingClientRect(),
            control = input.getBoundingClientRect();
          return (
            box.top >= field.top &&
            box.bottom <= field.bottom &&
            document.elementFromPoint(
              control.x + control.width / 2,
              control.y + control.height / 2,
            ) === input
          );
        });
      await expect.poll(acknowledgementFits).toBe(true);
      await shot("source-analysis-review");
      await expect.poll(acknowledgementFits).toBe(true);
      expect(reviews).toHaveLength(0);
      await acknowledge.click();
      await expect(
        dialog.getByText("reviewed · source inspection", { exact: true }),
      ).toBeVisible();
      expect(inspections).toHaveLength(1);
      expect(reviews).toHaveLength(1);
      await expect(dialog.getByRole("alert")).toHaveCount(0);
    });

// Controlled archive responses demonstrate identity retention, not native PostgreSQL acceptance.
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`guided fixture journey campaign archives ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const state = await setupCampaignHistory(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      state.postMode = "lost";
      state.archiveReadStatus = 404;
      await selectCampaignHistory(page, state);
      const dialog = await reviewCampaignSave(page);
      await expect(
        dialog.getByRole("button", { name: "Save report", exact: true }),
      ).toBeEnabled();
      await capture(page, "campaign-report-save-review", theme, device, {
        resetFields: false,
      });
      expect(state.archivePosts).toBe(0);
      await dialog
        .getByRole("button", { name: "Save report", exact: true })
        .click();
      await expect(
        page.getByText("Saved request unresolved", { exact: true }),
      ).toBeVisible();
      expect(state.archivePosts).toBe(1);
      expect(state.markerBeforePost).toBe(true);
      const original = { ...state.postBodies[0] };
      await page.reload();
      await navigate(page, "Product projects");
      await selectCampaignHistory(page, state);
      await page
        .getByRole("button", { name: "Check saved request 1", exact: true })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "No exact saved report was observed",
      );
      const align = async (selector: string) => {
        await page.locator(selector).evaluate((element) => {
          const main = element.closest("main")!,
            shell = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect();
          main.scrollTop +=
            element.getBoundingClientRect().top - shell.bottom - 16;
        });
        await expect
          .poll(() =>
            page.locator(selector).evaluate((element) => {
              const box = element.getBoundingClientRect(),
                shell = document
                  .querySelector(".workspace-main > header")!
                  .getBoundingClientRect();
              return (
                box.top >= shell.bottom + 12 &&
                element.contains(
                  document.elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  ),
                )
              );
            }),
          )
          .toBe(true);
      };
      await align(".campaign-history-pending h4");
      await capture(page, "campaign-report-save-unresolved", theme, device, {
        actionSelector: ".campaign-history-pending button",
      });
      expect(state.archivePosts).toBe(1);
      expect(state.archiveReads).toBe(1);
      state.archiveReadStatus = 200;
      await page
        .getByRole("button", { name: "Check saved request 1", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Saved campaign report",
          exact: true,
        }),
      ).toBeVisible();
      expect(state.archivePosts).toBe(1);
      expect(state.archiveReads).toBe(2);
      const receipt =
        state.archiveReceipt as typeof campaignArchiveFixture.receipt;
      expect(receipt.request_id).toBe(original.request_id);
      expect(receipt.report.digest).toBe(original.expected_report_digest);
      expect(receipt.actor_id).toBe("f".repeat(32));
      const summary = {
        ...campaignArchiveFixture.page.items[0],
        request_id: receipt.request_id,
        actor_id: receipt.actor_id,
        receipt_digest: receipt.digest,
        digest: "",
      };
      summary.digest = archiveFixtureDigest(
        Object.fromEntries(
          Object.entries(summary).filter(([key]) => key !== "digest"),
        ),
      );
      state.archivePage = { ...campaignArchiveFixture.page, items: [summary] };
      await page
        .getByRole("button", { name: "Load saved reports", exact: true })
        .click();
      await expect(
        page.getByRole("button", { name: "Open saved report 1", exact: true }),
      ).toBeVisible();
      await align(".campaign-report-history > .section-heading h3");
      await capture(page, "campaign-report-saved-history", theme, device, {
        actionSelector: ".campaign-history-list button",
      });
      await page
        .getByRole("button", { name: "Open saved report 1", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Saved campaign report",
          exact: true,
        }),
      ).toBeVisible();
      await align(
        '.campaign-report-history [aria-label="Exact saved report"] > h3',
      );
      await capture(page, "campaign-report-saved-receipt", theme, device, {
        actionSelector:
          '.campaign-report-history [aria-label="Exact saved report"] .campaign-report-components button',
      });
      expect(state.archivePosts).toBe(1);
      expect(state.archiveReads).toBe(3);
      expect(state.archiveLists).toBe(1);
      expect(state.postBodies).toEqual([original]);
    });

// These operator guides replay constructed API evidence; no agent, command or campaign executes.
async function alignOperatorGuide(
  page: import("@playwright/test").Page,
  selector: string,
) {
  await page.locator(selector).evaluate((element) => {
    const main = element.closest("main")!,
      shell = document
        .querySelector(".workspace-main > header")!
        .getBoundingClientRect();
    main.scrollTop += element.getBoundingClientRect().top - shell.bottom - 16;
  });
  await expect
    .poll(() =>
      page.locator(selector).evaluate((element) => {
        const box = element.getBoundingClientRect(),
          shell = document
            .querySelector(".workspace-main > header")!
            .getBoundingClientRect();
        return (
          box.top >= shell.bottom + 8 &&
          element.contains(
            document.elementFromPoint(
              box.x + box.width / 2,
              box.y + box.height / 2,
            ),
          )
        );
      }),
    )
    .toBe(true);
}
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`guided fixture journey agent answers ${device} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const question = structuredClone(workerContract.feedback),
        guideRun = {
          id: question.run_id,
          repository_id: "3".repeat(32),
          project_id: null,
          brief: "Fixture inventory decision",
          status: "paused",
          publish: false,
          pause: {
            kind: "worker_request",
            automatic: false,
            message: question.payload.summary,
          },
        };
      let response: Record<string, unknown> | null = null,
        original: Record<string, string> | null = null,
        readStatus = 200,
        includeAnswer = false;
      const posts: Record<string, string>[] = [],
        reads: string[] = [];
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        if (path.endsWith("/respond")) {
          expect(request.method()).toBe("POST");
          expect(path).toBe(
            `/api/v1/runs/${guideRun.id}/worker-requests/${question.payload.request.id}/respond`,
          );
          original = request.postDataJSON();
          posts.push(original!);
          expect(Object.keys(original!).sort()).toEqual([
            "actor",
            "answer",
            "id",
          ]);
          expect(original!.answer).toBe(
            "Reserve up to **14 items**.\n\nKeep the reservation atomic.",
          );
          const stored = await page.evaluate(() =>
            Object.entries(localStorage).filter(([key]) =>
              key.startsWith("forge.worker-reply.v1:"),
            ),
          );
          expect(stored).toHaveLength(1);
          expect(stored[0][1]).toContain(original!.id);
          expect(stored[0][1]).not.toContain(original!.answer);
          const body = {
            ...workerContract.legacy_response,
            idempotency_key: `worker-response:${original!.id}`,
            payload: {
              ...original!,
              request_id: question.payload.request.id,
              responding_actor: {
                version: 1,
                user_id: "f".repeat(32),
                authentication_method: "local_bootstrap",
              },
            },
          };
          response = {
            ...body,
            digest: archiveFixtureDigest(
              Object.fromEntries(
                Object.entries(body).filter(([key]) => key !== "digest"),
              ),
            ),
          };
          return route.abort("failed");
        }
        expect(request.method()).toBe("GET");
        if (path.endsWith("/worker-messages")) {
          reads.push(path);
          return route.fulfill({
            status: readStatus,
            json:
              readStatus === 200
                ? includeAnswer
                  ? [question, response]
                  : [question]
                : { detail: "Conversation not observed" },
          });
        }
        const json = path.endsWith("/repositories")
          ? [
              {
                id: guideRun.repository_id,
                name: "Fixture inventory",
                provider: "git",
                url: "/local/fixture-inventory",
                default_branch: "main",
              },
            ]
          : path.endsWith("/runs")
            ? [guideRun]
            : path.endsWith(`/runs/${guideRun.id}`)
              ? guideRun
              : path.endsWith("/health")
                ? {
                    status: "ok",
                    service_role: "api",
                    admission_ready: true,
                    execution_ready: false,
                    execution: { local_enabled: false },
                  }
                : [];
        return route.fulfill({ json });
      });
      const openConversation = async () => {
        await navigate(page, "Factory");
        await page
          .getByRole("button", { name: /Fixture inventory decision/ })
          .click();
        await page
          .getByRole("tab", { name: "Agent questions", exact: true })
          .click();
      };
      const shot = async (
        stage: string,
        selector: string,
        actionSelector?: string,
      ) => {
        await alignOperatorGuide(page, selector);
        await capture(page, stage, theme, device, { actionSelector });
        await page.screenshot({ path: testInfo.outputPath(stage + ".png") });
      };
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("guide-operator-token-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await openConversation();
      await expect(
        page.getByRole("heading", {
          name: question.payload.request.question,
          exact: true,
        }),
      ).toBeVisible();
      await page.getByText("Why the agent is asking", { exact: true }).click();
      await expect(
        page.getByText(question.payload.request.context, { exact: true }),
      ).toBeVisible();
      await page.getByRole("button", { name: "14 items", exact: true }).click();
      await page
        .getByLabel("Your answer", { exact: true })
        .fill("Reserve up to **14 items**.\n\nKeep the reservation atomic.");
      await shot(
        "agent-question",
        ".worker-thread h3",
        ".worker-answer-form button.primary",
      );
      expect(posts).toHaveLength(0);
      await page
        .getByRole("button", { name: "Review answer", exact: true })
        .click();
      await expect(
        page.getByRole("heading", { name: "Review your answer", exact: true }),
      ).toBeFocused();
      await shot(
        "agent-answer-review",
        ".worker-answer-review h4",
        ".worker-answer-review button.primary",
      );
      await page
        .getByRole("button", { name: "Send answer", exact: true })
        .click();
      await expect(
        page.getByText(
          "Delivery is unconfirmed. Check the original answer; another answer will not be sent automatically.",
          { exact: true },
        ),
      ).toBeVisible();
      expect(posts).toHaveLength(1);
      readStatus = 404;
      const beforeMissingRead = reads.length;
      await page
        .getByRole("button", { name: "Check original answer", exact: true })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "The conversation could not be confirmed",
      );
      expect(reads).toHaveLength(beforeMissingRead + 1);
      const marker = await page.evaluate(() =>
        Object.entries(localStorage).filter(([key]) =>
          key.startsWith("forge.worker-reply.v1:"),
        ),
      );
      readStatus = 200;
      await page.reload();
      await openConversation();
      await expect(
        page.getByText(
          "No matching answer is currently available. Keep the original request; this does not prove it was not sent.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(page.getByLabel("Your answer", { exact: true })).toHaveCount(
        0,
      );
      await shot(
        "agent-answer-unresolved",
        ".worker-dialogue h2",
        ".worker-dialogue-heading button",
      );
      expect(
        await page.evaluate(() =>
          Object.entries(localStorage).filter(([key]) =>
            key.startsWith("forge.worker-reply.v1:"),
          ),
        ),
      ).toEqual(marker);
      includeAnswer = true;
      const beforeConfirmedRead = reads.length;
      await page
        .getByRole("button", { name: "Check original answer", exact: true })
        .click();
      await expect(
        page.getByText(
          "Answer recorded. The run stays paused until you resume it.",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        page.getByRole("region", { name: "Recorded answer", exact: true }),
      ).toContainText("14 items");
      await expect(
        page.getByText(
          "The server also recorded the authenticated user identity.",
          { exact: false },
        ),
      ).toBeVisible();
      await expect(page.getByRole("alert")).toHaveCount(0);
      await shot("agent-answer-confirmed", ".worker-recorded-answer h4");
      expect(posts).toEqual([original]);
      expect(reads).toHaveLength(beforeConfirmedRead + 1);
      expect(new Set(reads).size).toBe(1);
      await expect(
        page.getByRole("button", { name: "Send answer", exact: true }),
      ).toHaveCount(0);
    });

for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`guided fixture journey reporting plans ${device} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const state = await setupDelivery(page);
      await page.route("**/api/v1/health", (route) =>
        route.fulfill({
          json: {
            status: "ok",
            service_role: "api",
            admission_ready: true,
            execution_ready: false,
            execution: { local_enabled: false },
          },
        }),
      );
      await page.reload();
      await navigate(page, "Product projects");
      await selectCampaignHistory(page, state.base);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      state.postMode = "lost";
      state.readStatus = 404;
      const shot = async (
        stage: string,
        resetFields = true,
        actionSelector?: string,
      ) => {
        await capture(page, stage, theme, device, {
          resetFields,
          actionSelector,
        });
        await page.screenshot({ path: testInfo.outputPath(stage + ".png") });
      };
      await page
        .getByRole("button", { name: "View report", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Review a reporting plan", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Review reporting plan",
        exact: true,
      });
      await dialog
        .getByLabel("Plan name", { exact: true })
        .fill("Inventory delivery");
      await dialog
        .getByRole("button", { name: "Add wave", exact: true })
        .click();
      await dialog.getByLabel("Wave 1 name", { exact: true }).fill("Core API");
      await dialog
        .getByRole("checkbox", { name: "Frozen inventory API", exact: true })
        .check();
      await shot("campaign-plan-waves");
      await dialog
        .getByRole("button", { name: "Continue to milestones", exact: true })
        .click();
      await dialog
        .getByRole("button", { name: "Add milestone to Core API", exact: true })
        .click();
      await dialog
        .getByLabel("Milestone 1 name in Core API", { exact: true })
        .fill("Inventory checked");
      await expect(
        dialog.getByRole("combobox", {
          name: "Evidence required",
          exact: true,
        }),
      ).toHaveValue("component_verified");
      await shot("campaign-plan-milestones");
      await dialog
        .getByRole("button", { name: "Review reporting criteria", exact: true })
        .click();
      await dialog
        .getByRole("checkbox", {
          name: "I understand: reporting only.",
          exact: true,
        })
        .check();
      await shot("campaign-plan-review", false);
      expect(state.posts).toHaveLength(0);
      await dialog
        .getByRole("button", { name: "Save reporting plan", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Original reporting-plan request retained",
          exact: true,
        }),
      ).toBeVisible();
      expect(state.posts).toHaveLength(1);
      expect(state.markerBefore).toBe(true);
      const original = structuredClone(state.posts[0]);
      await page.reload();
      await navigate(page, "Product projects");
      await selectCampaignHistory(page, state.base);
      await page
        .getByRole("button", {
          name: "Check original reporting plan",
          exact: true,
        })
        .click();
      await expect(page.getByRole("alert")).toContainText(
        "The reporting plan or progress could not be confirmed",
      );
      await alignOperatorGuide(
        page,
        ".campaign-delivery-plans .campaign-history-pending h4",
      );
      await shot(
        "campaign-plan-unresolved",
        true,
        ".campaign-delivery-plans .campaign-history-pending button",
      );
      state.readStatus = 200;
      await page
        .getByRole("button", {
          name: "Check original reporting plan",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Reporting plan confirmed",
          exact: true,
        }),
      ).toBeVisible();
      await page
        .getByRole("button", { name: "Read reporting plans", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Open reporting plan 1", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Load saved reports", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Open saved report 1", exact: true })
        .click();
      await page
        .getByRole("button", { name: "Read delivery progress", exact: true })
        .click();
      const progress = page.getByRole("region", {
        name: "Saved-cut delivery progress",
        exact: true,
      });
      await expect(
        progress.getByRole("heading", {
          name: "Delivery progress: in progress",
          exact: true,
        }),
      ).toBeVisible();
      await progress
        .getByText("Core API · in progress", { exact: true })
        .click();
      await progress
        .getByText("Inventory checked · in progress", { exact: true })
        .click();
      await expect(
        progress.getByText("Component verified", { exact: false }).first(),
      ).toBeVisible();
      await expect(page.getByRole("alert")).toHaveCount(0);
      await alignOperatorGuide(
        page,
        '.campaign-delivery-plans [aria-label="Saved-cut delivery progress"] > h4',
      );
      await shot("campaign-plan-progress");
      expect(state.posts).toEqual([original]);
      expect(state.reads).toBe(3);
      expect(state.progressReads).toBe(1);
      expect(state.base.archivePosts).toBe(0);
      expect(state.base.writes).toBe(0);
      expect(
        state.receipts.get(original.request_id as string)?.request_digest,
      ).toBe(archiveFixtureDigest(original));
    });

for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`guided fixture journey SCM delegation ${device} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      // Constructed from the strict e9 SCM models; this guide performs no provider delivery or grant check.
      const projectId = "5".repeat(32),
        repositoryId = "6".repeat(32),
        nativeUser = "3".repeat(32);
      const definition = {
        name: "Inventory product",
        description: "Controlled delegation walkthrough",
        owners: ["Fixture owner"],
        topology: "monorepo",
        roles: null,
        product_repository_id: repositoryId,
        components: [
          {
            id: "api",
            name: "Inventory API",
            repository_id: repositoryId,
            path: ".",
            kind: "api",
            backend: "pyfly",
            frontend: "none",
            verification: [],
            depends_on: [],
            responsibility: "Inventory requests",
          },
        ],
      };
      const project = {
        id: projectId,
        revision: 1,
        definition,
        digest: archiveFixtureDigest(definition),
        created_at: "2026-10-15T00:00:00Z",
        updated_at: "2026-10-15T00:00:00Z",
      };
      const cut = archiveFixtureDigest(project),
        candidateId = "2".repeat(32);
      const reference = {
        version: 1,
        provider: "github",
        api_origin: "https://api.github.com",
        installation_id: "17",
        remote_repository_id: "42",
        external_actor_id: "7",
        web_origin: "https://github.com",
        connection_revision: "a".repeat(32),
        connection_scope_digest: "b".repeat(64),
        webhook_key_revision: "c".repeat(32),
        verification_method: "raw_body_hmac_sha256",
        payload_digest: "d".repeat(64),
      };
      const referenceDigest = archiveFixtureDigest(reference);
      let receipt: Record<string, unknown> | null = null,
        original: Record<string, unknown> | null = null;
      const posts: Record<string, unknown>[] = [],
        exactReads: string[] = [];
      const base = `/api/v1/projects/${projectId}/repositories/${repositoryId}/scm-actor-bindings`;
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          url = new URL(request.url()),
          path = url.pathname;
        if (path === base && request.method() === "POST") {
          original = request.postDataJSON();
          posts.push(original!);
          expect(original).toEqual({
            version: 1,
            request_id: expect.stringMatching(/^[a-f0-9]{32}$/),
            expected_previous_digest: null,
            expected_project_binding_digest: cut,
            verified_receipt_id: candidateId,
            expected_verified_reference_digest: referenceDigest,
            native_user_id: nativeUser,
            component_ids: ["api"],
            allowed_actions: ["respond"],
            active: true,
            acknowledge_webhook_asserted_identity: true,
          });
          const retained = await page.evaluate(() =>
            Object.entries(localStorage)
              .filter(([key]) => key.startsWith("forge.scm-delegation.v1:"))
              .map(([, value]) => JSON.parse(value)),
          );
          expect(retained).toEqual([
            {
              version: 1,
              base: new URL("/api/v1", page.url()).href,
              actor: "f".repeat(32),
              project: projectId,
              repository: repositoryId,
              requestId: original!.request_id,
              inputDigest: archiveFixtureDigest(original),
            },
          ]);
          const body = {
            version: 1,
            event_id: "4".repeat(32),
            project_id: projectId,
            repository_id: repositoryId,
            project_binding_digest: cut,
            binding_key: archiveFixtureDigest({
              project_id: projectId,
              provider: reference.provider,
              api_origin: reference.api_origin,
              installation_id: reference.installation_id,
              remote_repository_id: reference.remote_repository_id,
              external_actor_id: reference.external_actor_id,
            }),
            revision: 1,
            previous_digest: null,
            prepared_at: "2026-10-15T19:00:00+00:00",
            administering_user_id: "f".repeat(32),
            request: original,
            request_digest: archiveFixtureDigest(original),
            verified_reference: reference,
          };
          receipt = { ...body, digest: archiveFixtureDigest(body) };
          return route.fulfill({ status: 201, json: receipt });
        }
        if (path.startsWith(base + "/requests/")) {
          expect(request.method()).toBe("GET");
          const id = path.split("/").at(-1)!;
          exactReads.push(id);
          expect(id).toBe(original?.request_id);
          return route.fulfill({
            status: receipt ? 200 : 404,
            json: receipt ?? { detail: "Original receipt is unavailable" },
          });
        }
        expect(request.method()).toBe("GET");
        if (path === base + "/candidates")
          return route.fulfill({
            json: {
              version: 1,
              project_id: projectId,
              repository_id: repositoryId,
              project_binding_digest: cut,
              items: [
                {
                  verified_receipt_id: candidateId,
                  verified_reference: reference,
                  verified_reference_digest: referenceDigest,
                },
              ],
              next_cursor: null,
            },
          });
        if (path === base)
          return route.fulfill({
            json: {
              version: 1,
              project_id: projectId,
              repository_id: repositoryId,
              project_binding_digest: cut,
              heads_only: true,
              items: [],
              next_cursor: null,
            },
          });
        if (path.endsWith("/eligible-users"))
          return route.fulfill({
            json: [
              {
                id: nativeUser,
                display_name: "Warehouse reviewer",
                enabled: true,
                email: null,
                contact_email: null,
              },
            ],
          });
        return route.fulfill({
          json: path.endsWith("/projects")
            ? [project]
            : path.endsWith("/repositories")
              ? [
                  {
                    id: repositoryId,
                    name: "Inventory repository",
                    url: "https://github.example.test/fixture/inventory",
                  },
                ]
              : path.endsWith("/health")
                ? {
                    status: "ok",
                    service_role: "api",
                    admission_ready: true,
                    execution_ready: false,
                    detail: "",
                    execution: { local_enabled: false },
                  }
                : path.endsWith("/profiles")
                  ? {}
                  : [],
        });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("guide-operator-token-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await navigate(page, "Project access");
      await page
        .getByRole("button", { name: "Review a delegation", exact: true })
        .click();
      const dialog = page.getByRole("dialog", {
        name: "Review SCM delegation",
        exact: true,
      });
      const shot = async (
        stage: string,
        resetFields = true,
        actionSelector?: string,
      ) => {
        await capture(page, stage, theme, device, {
          resetFields,
          actionSelector,
        });
        await page.screenshot({ path: testInfo.outputPath(stage + ".png") });
      };
      await dialog
        .getByLabel("Webhook-reported account", { exact: true })
        .selectOption(candidateId);
      await expect(
        dialog.getByText(/not a personal OAuth login/),
      ).toBeVisible();
      await shot("scm-delegation-account");
      await dialog
        .getByRole("button", { name: "Continue to scope", exact: true })
        .click();
      await dialog
        .getByLabel("Find a registered person", { exact: true })
        .fill("Warehouse");
      await dialog
        .getByRole("button", { name: "Find person", exact: true })
        .click();
      await dialog
        .getByLabel("Registered person", { exact: true })
        .selectOption(nativeUser);
      await dialog
        .getByRole("checkbox", { name: "Inventory API", exact: true })
        .check();
      await dialog
        .getByRole("checkbox", { name: "Answer agent questions", exact: true })
        .check();
      await dialog
        .getByLabel("Registered person", { exact: true })
        .locator("..")
        .evaluate((node) => node.scrollIntoView({ block: "start" }));
      await shot("scm-delegation-scope", false);
      await dialog
        .getByRole("button", { name: "Review delegation", exact: true })
        .click();
      await dialog
        .getByRole("checkbox", {
          name: "I trust this webhook assertion.",
          exact: true,
        })
        .check();
      expect(posts).toHaveLength(0);
      await shot("scm-delegation-review", false);
      await dialog
        .getByRole("button", { name: "Save delegation", exact: true })
        .click();
      const confirmed = page.getByRole("region", {
        name: "Exact delegation receipt",
        exact: true,
      });
      await expect(confirmed).toBeVisible();
      await expect(
        page.getByText("Exact receipt confirmed", { exact: true }),
      ).toBeVisible();
      expect(posts).toHaveLength(1);
      expect(exactReads).toEqual([original!.request_id]);
      await expect(page.getByRole("alert")).toHaveCount(0);
      await alignOperatorGuide(
        page,
        device === "desktop"
          ? ".scm-delegation-recovery > h3"
          : ".scm-delegation-recovery > div > p",
      );
      await shot(
        "scm-delegation-confirmed",
        true,
        ".scm-delegation-receipt > button",
      );
      const confirmedGeometry = await confirmed.evaluate((region) => {
        const shell = document
            .querySelector(".workspace-main > header")!
            .getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect(),
          confirmation = document.querySelector(
            ".scm-delegation-recovery > div > p",
          )!,
          button = region.querySelector("button")!;
        const painted = (element: Element) => {
          const box = element.getBoundingClientRect();
          return {
            top: box.top,
            bottom: box.bottom,
            hit: element.contains(
              document.elementFromPoint(
                box.x + box.width / 2,
                box.y + box.height / 2,
              ),
            ),
          };
        };
        return {
          shellBottom: shell.bottom,
          footerTop: footer.top,
          confirmation: painted(confirmation),
          recoveryHeading: painted(
            document.querySelector(".scm-delegation-recovery > h3")!,
          ),
          receipt: painted(region),
          close: painted(button),
        };
      });
      await testInfo.attach("confirmed-geometry", {
        body: JSON.stringify(confirmedGeometry, null, 2),
        contentType: "application/json",
      });
      expect(confirmedGeometry.confirmation.top).toBeGreaterThanOrEqual(
        confirmedGeometry.shellBottom + 8,
      );
      expect(confirmedGeometry.receipt.top).toBeGreaterThanOrEqual(
        confirmedGeometry.shellBottom + 8,
      );
      expect(confirmedGeometry.receipt.bottom).toBeLessThanOrEqual(
        confirmedGeometry.footerTop - 8,
      );
      expect(confirmedGeometry.confirmation.hit).toBe(true);
      expect(confirmedGeometry.close.hit).toBe(true);
      if (device === "desktop")
        expect(confirmedGeometry.recoveryHeading.top).toBeGreaterThanOrEqual(
          confirmedGeometry.shellBottom + 8,
        );
      expect(posts).toEqual([original]);
      expect(exactReads).toHaveLength(1);
      expect(receipt?.request_digest).toBe(archiveFixtureDigest(original));
    });

// Controlled recovery guides share the regression fixtures; no native run or model executes.
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"] as const) {
    test(`guided fixture journey original run request ${device} ${theme}`, async ({
      page,
      context,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const fixture = await setupRunCreation(context);
      await openRunCreation(page, true);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await submitRunCreation(
        page,
        "Keep fictional inventory reservations within available stock.",
      );
      const [original] = await runCreationMarkers(page);
      const panel = page.getByRole("region", {
        name: "Original run request",
        exact: true,
      });
      await panel
        .getByRole("button", { name: "Check original result", exact: true })
        .click();
      await expect(
        panel.getByRole("button", {
          name: "Send original request again",
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        panel.getByRole("button", { name: "Cancel this request", exact: true }),
      ).toBeVisible();
      expect((await runCreationMarkers(page))[0].requestId).toBe(
        original.requestId,
      );
      expect(
        fixture.calls.filter((call) => call.action === "original"),
      ).toHaveLength(1);
      await expect(panel.locator("dd code").first()).toHaveText(
        original.requestId,
      );
      expect(
        await panel.evaluate((element) => {
          const scroll = element.closest(".wizard-field-scroll")!;
          return (
            element.scrollWidth <= element.clientWidth &&
            scroll.scrollWidth <= scroll.clientWidth &&
            document.documentElement.scrollWidth <= innerWidth &&
            (innerWidth > 600 ||
              getComputedStyle(scroll).overflowY === "visible")
          );
        }),
      ).toBe(true);
      for (const button of await panel.locator(":scope > button").all()) {
        await button.scrollIntoViewIfNeeded();
        await button.focus();
        await expect(button).toBeFocused();
        const geometry = await button.evaluate((element) => {
          const box = element.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect(),
            footer = document
              .querySelector(".workspace-main > footer")!
              .getBoundingClientRect();
          return {
            inFrame:
              box.left >= 0 &&
              box.right <= innerWidth &&
              box.top >= header.bottom &&
              box.bottom <= footer.top,
            hit: element.contains(
              document.elementFromPoint(
                box.x + box.width / 2,
                box.y + box.height / 2,
              ),
            ),
            touchHeight: box.height >= 44,
          };
        });
        expect(geometry).toEqual({
          inFrame: true,
          hit: true,
          touchHeight: true,
        });
      }
      await capture(page, "recovery-run-request-held", theme, device, {
        resetFields: false,
        actionSelector: '[aria-label="Original run request"] > button',
      });
      for (let step = 0; step < 3; step++)
        await page.getByRole("button", { name: "Back", exact: true }).click();
      const editedBrief =
        "Keep my revised fictional reservation rules for the next request.";
      await page
        .getByLabel("What should this change accomplish?", { exact: true })
        .fill(editedBrief);
      await panel
        .getByRole("button", { name: "Cancel this request", exact: true })
        .click();
      await expect(
        page.getByText("Request cancelled. You can edit and start again.", {
          exact: true,
        }),
      ).toBeVisible();
      await expect(
        page.getByLabel("What should this change accomplish?", { exact: true }),
      ).toHaveValue(editedBrief);
      expect(await runCreationMarkers(page)).toEqual([]);
      expect(fixture.calls.map((call) => call.action)).toEqual([
        "original",
        "decision",
        "cancel",
      ]);
      await expect(
        page.getByRole("button", { name: "Continue", exact: true }),
      ).toBeEnabled();
      await capture(page, "recovery-run-request-cancelled", theme, device, {
        resetFields: false,
      });
    });

    test(`guided fixture journey original model result ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const state = await setupOriginalResult(page, "restore", "lost", theme);
      const panel = page.getByRole("region", {
        name: "Original model result",
        exact: true,
      });
      const review = panel.getByRole("button", {
        name: "Review original result",
        exact: true,
      });
      await expect(
        panel.getByLabel("Original implementation attempt"),
      ).toBeVisible();
      await expect(
        panel.getByText("Original result saved", { exact: true }),
      ).toBeVisible();
      expect(state.posts).toBe(0);
      await alignOperatorGuide(page, ".agent-checkpoint-recovery h2");
      await capture(page, "recovery-original-result-ready", theme, device, {
        actionSelector: ".agent-checkpoint-recovery > button",
      });
      await review.focus();
      await page.keyboard.press("Enter");
      await expect(
        panel.getByRole("heading", {
          name: "Review continuation",
          exact: true,
        }),
      ).toBeFocused();
      const continuation = panel.getByRole("button", {
        name: "Continue from original result",
        exact: true,
      });
      await expect(continuation).toBeDisabled();
      await panel.getByRole("checkbox").check();
      await expect(continuation).toBeEnabled();
      expect(state.posts).toBe(0);
      await capture(page, "recovery-original-result-review", theme, device, {
        actionSelector: ".agent-checkpoint-review button.primary",
      });
      await continuation.click();
      await expect(
        page.getByText(/Existing request identities stay held/),
      ).toBeVisible();
      const requestId = state.request;
      await panel
        .getByRole("button", { name: "Read exact saved request", exact: true })
        .click();
      await expect(
        page.getByText(/This does not prove the request was not accepted/),
      ).toBeVisible();
      expect(state.posts).toBe(1);
      expect(state.reads).toBe(1);
      expect(state.request).toBe(requestId);
      const saved = await page.evaluate(
        () =>
          Object.entries(sessionStorage).find(([key]) =>
            key.startsWith("forge.agent-checkpoint.v1:"),
          )?.[1],
      );
      expect(saved).toContain(requestId);
      await expect(
        panel.getByRole("button", {
          name: "Continue from original result",
          exact: true,
        }),
      ).toHaveCount(0);
      await capture(page, "recovery-original-result-held", theme, device, {
        actionSelector: ".agent-checkpoint-recovery .card-actions button",
        focusSelector: ".agent-checkpoint-recovery",
      });
      expect(
        await panel.evaluate((element) => {
          const box = element.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect(),
            footer = document
              .querySelector(".workspace-main > footer")!
              .getBoundingClientRect();
          return (
            box.height > footer.top - header.bottom - 32 ||
            box.top >= header.bottom + 8
          );
        }),
      ).toBe(true);

      const provider = {
        id: "bridge",
        name: "Bridge",
        ready: true,
        detail: "Controlled recovery configuration fixture",
        config: { model: "bridge/default" },
        fields: ["model", "completion_recovery", "checkpoints"].map((key) => ({
          key,
          label: key === "model" ? "Model" : key,
          secret: false,
          required: false,
          placeholder: "",
        })),
      };
      let configurationWrites = 0,
        probes = 0;
      await page.route("**/api/v1/providers**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/test")) probes++;
        if (route.request().method() !== "GET") configurationWrites++;
        await route.fulfill({
          json: path.endsWith("/providers") ? [provider] : provider,
        });
      });
      await page.reload();
      await navigate(page, "Connections");
      await page
        .getByRole("button", { name: "Configure", exact: true })
        .click();
      await expect(
        page.getByLabel("Original model result recovery", { exact: true }),
      ).toHaveValue("");
      await expect(
        page.getByLabel("Implementation checkpoints", { exact: true }),
      ).toHaveValue("");
      await page
        .getByLabel("Original model result recovery", { exact: true })
        .selectOption("required");
      await page
        .getByLabel("Implementation checkpoints", { exact: true })
        .selectOption("required");
      const choices = page.getByRole("group", {
        name: "Original model results",
        exact: true,
      });
      for (const select of await choices.locator("select").all()) {
        await expect(select.locator("option:checked")).toHaveText("Required");
        await select.scrollIntoViewIfNeeded();
        await select.focus();
        await expect(select).toBeFocused();
        expect(
          await select.evaluate((element) => {
            const style = getComputedStyle(element),
              context = document.createElement("canvas").getContext("2d")!,
              box = element.getBoundingClientRect();
            context.font = style.font;
            return (
              context.measureText(element.selectedOptions[0].textContent!)
                .width <=
                element.clientWidth -
                  parseFloat(style.paddingLeft) -
                  parseFloat(style.paddingRight) &&
              element.contains(
                document.elementFromPoint(
                  box.x + box.width / 2,
                  box.y + box.height / 2,
                ),
              )
            );
          }),
        ).toBe(true);
      }
      const choicesFit = await choices.evaluate((element) => {
        const scroll = element.closest(".wizard-field-scroll")!,
          header = document
            .querySelector(".workspace-main > header")!
            .getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
        return (
          element.getBoundingClientRect().height <=
          Math.min(
            footer.top - header.bottom - 32,
            scroll.scrollHeight > scroll.clientHeight
              ? scroll.clientHeight - 16
              : Infinity,
          )
        );
      });
      await capture(page, "recovery-bridge-choices", theme, device, {
        actionSelector: choicesFit
          ? ".bridge-recovery-choices"
          : ".bridge-recovery-choices .field",
        focusSelector: ".bridge-recovery-choices",
        resetFields: false,
      });
      expect(
        await choices.evaluate((element, fit) => {
          const box = element.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect(),
            footer = document
              .querySelector(".workspace-main > footer")!
              .getBoundingClientRect();
          return (
            box.left >= 0 &&
            box.right <= innerWidth &&
            (!fit || (box.top >= header.bottom && box.bottom <= footer.top)) &&
            element.scrollWidth <= element.clientWidth &&
            document.documentElement.scrollWidth <= innerWidth
          );
        }, choicesFit),
      ).toBe(true);
      expect(configurationWrites).toBe(0);
      expect(probes).toBe(0);
    });
  }
