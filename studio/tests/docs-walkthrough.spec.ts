/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { captureWalkthrough as capture } from "./walkthrough-capture";
import { record as learningRecord } from "./learning-record-fixture";

// Route fixtures exercise Studio presentation; no provider or knowledge service runs.
const repository = {
  id: "fixture-repo",
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
  id: "fixture-run",
  repository_id: repository.id,
  brief: "Fixture reservation verification",
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
      url: "/api/v1/runs/fixture-run/artifacts/changes.zip",
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
          json = [learningRecord, failureCandidate];
        else if (
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
        else if (path.endsWith("/product")) {
          if (request.method() === "PUT")
            product = {
              repository_id: repository.id,
              revision: 1,
              definition: request.postDataJSON(),
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
        else if (path.endsWith("/runs")) {
          if (request.method() === "POST") {
            admitted = true;
            json = run;
          } else json = admitted ? [run] : [];
        } else if (path.endsWith("/runs/fixture-run")) json = run;
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
        .fill("Implement stock reservation and reject over-reservation");
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
      await navigate(page, "Factory");
      await capture(page, "factory-overview", theme, device);
      await page
        .getByRole("button", { name: /Fixture reservation verification/ })
        .click();
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
        page.getByText("Your run has been queued.", { exact: true }),
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
    });
  }
