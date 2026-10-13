/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

// Route fixtures exercise Studio presentation; no provider or knowledge service runs.
const repository = {
  id: "fixture-repo",
  name: "Fixture Inventory API",
  provider: "git",
  url: "/local/fixture-inventory",
  default_branch: "main",
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
const digest = (data: string | Buffer) =>
  createHash("sha256").update(data).digest("hex");
const root = fileURLToPath(new URL("../..", import.meta.url));
const assets = resolve(root, "docs/assets/screenshots");
function sources(directory: string): Record<string, string> {
  return Object.assign(
    {},
    ...readdirSync(directory, { withFileTypes: true }).map((entry) => {
      const path = resolve(directory, entry.name);
      return entry.isDirectory()
        ? sources(path)
        : { [relative(root, path)]: digest(readFileSync(path)) };
    }),
  );
}
async function capture(
  page: Page,
  stage: string,
  theme: string,
  device: string,
) {
  await page.evaluate(() => document.fonts.ready);
  if (
    !stage.startsWith("evidence-") &&
    ![
      "product-behavior",
      "product-feature-mapping",
      "run-review-provider",
      "run-policies",
    ].includes(stage)
  )
    await page
      .locator(".wizard-field-scroll")
      .evaluateAll((nodes) => nodes.forEach((node) => (node.scrollTop = 0)));
  const expectedStep: Record<string, string> = {
    "connect-workspace": "Workspace",
    "connect-coding-ai": "Coding AI",
    "connect-repositories": "Repositories",
    "connect-knowledge": "Knowledge",
    "product-context": "Context",
    "product-journeys": "Journeys",
    "product-features": "Features",
    "product-behavior": "Features",
    "product-feature-mapping": "Features",
    "product-integrations": "Integrations",
    "product-review": "Review",
    "run-brief": "Brief",
    "run-foundation": "Foundation",
    "run-checks": "Checks",
    "run-review-provider": "Checks",
    "run-policies": "Checks",
    "run-review": "Review",
  };
  if (expectedStep[stage])
    await expect(
      page.locator('.wizard-steps [aria-current="step"]'),
    ).toContainText(expectedStep[stage]);
  const action = page.locator(".wizard-actions button.primary").last();
  let control = null;
  if (await action.isVisible()) {
    await action.scrollIntoViewIfNeeded();
    control = await action.evaluate((button) => {
      const box = button.getBoundingClientRect();
      const footer = document
        .querySelector(".workspace-main > footer")!
        .getBoundingClientRect();
      return {
        label: button.textContent?.trim(),
        top: box.top,
        bottom: box.bottom,
        footerTop: footer.top,
        hit:
          document
            .elementFromPoint(box.x + box.width / 2, box.bottom - 2)
            ?.closest("button") === button,
      };
    });
    expect(control.top).toBeGreaterThanOrEqual(0);
    expect(control.bottom).toBeLessThanOrEqual(control.footerTop);
    expect(control.hit).toBe(true);
  }
  if (stage === "product-feature-mapping") {
    await page
      .getByLabel("Feature name", { exact: true })
      .evaluate((input) => input.scrollIntoView({ block: "center" }));
    await expect(page.getByLabel("Feature name", { exact: true })).toHaveValue(
      "Reserve available stock",
    );
    const field = await page
      .getByLabel("Feature name", { exact: true })
      .evaluate((input) => {
        const box = input.getBoundingClientRect();
        return (
          document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          ) === input
        );
      });
    expect(field).toBe(true);
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  if (!process.env.UPDATE_DOC_SCREENSHOTS) return;
  const selected = process.env.CAPTURE_STAGES?.split(",");
  if (selected && !selected.includes(stage)) return;
  mkdirSync(assets, { recursive: true });
  const name = `${stage}-${device}-${theme}.png`;
  const png = await page.screenshot({
    path: resolve(assets, name),
    fullPage: false,
  });
  const capturedAction = (await action.isVisible())
    ? await action.evaluate((button) => {
        const box = button.getBoundingClientRect(),
          footer = document
            .querySelector(".workspace-main > footer")!
            .getBoundingClientRect();
        return {
          top: box.top,
          bottom: box.bottom,
          footerTop: footer.top,
          in_frame: box.top >= 0 && box.bottom <= footer.top,
          hit:
            document
              .elementFromPoint(box.x + box.width / 2, box.bottom - 2)
              ?.closest("button") === button,
        };
      })
    : null;
  if (capturedAction?.in_frame) expect(capturedAction.hit).toBe(true);
  const manifestPath = resolve(assets, "walkthrough-manifest.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  manifest.studio_version = JSON.parse(
    readFileSync(resolve(root, "studio/package.json"), "utf8"),
  ).version;
  const currentVersion = JSON.parse(
    readFileSync(resolve(root, "studio/package.json"), "utf8"),
  ).version;
  await expect(page.locator(".workspace-main > footer")).toContainText(
    "v" + currentVersion,
  );
  const currentSource = {
    ...sources(resolve(root, "studio/src")),
    ...sources(resolve(root, "studio/public")),
    "studio/index.html": digest(
      readFileSync(resolve(root, "studio/index.html")),
    ),
    "studio/package.json": digest(
      readFileSync(resolve(root, "studio/package.json")),
    ),
  };
  const sourceDigest = (value: Record<string, string>) =>
    digest(
      JSON.stringify(
        Object.fromEntries(
          Object.entries(value).sort(([a], [b]) =>
            a < b ? -1 : a > b ? 1 : 0,
          ),
        ),
      ),
    );
  for (const record of Object.values(manifest.captures) as Record<
    string,
    unknown
  >[]) {
    record.capture_test_sha256 ??= manifest.capture_test_sha256;
    record.studio_version ??= manifest.studio_version;
    record.frontend_digest ??= sourceDigest(manifest.frontend);
  }
  manifest.captures[name] = {
    capture_test_sha256: digest(
      readFileSync(resolve(root, "studio/tests/docs-walkthrough.spec.ts")),
    ),
    studio_version: currentVersion,
    frontend_digest: sourceDigest(currentSource),
    path: `assets/screenshots/${name}`,
    stage,
    theme,
    fixture: true,
    viewport: page.viewportSize(),
    sha256: digest(png),
    action: control,
    captured_action: capturedAction,
  };
  manifest.frontend = currentSource;
  manifest.capture_test_sha256 = digest(
    readFileSync(resolve(root, "studio/tests/docs-walkthrough.spec.ts")),
  );
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
}
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
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        let json: unknown = {};
        if (path.endsWith("/repositories")) json = [repository];
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
    });
  }
