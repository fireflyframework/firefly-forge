/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import type {
  CustomerProductDefinition,
  FunctionalFeature,
  ProductSnapshot,
} from "../src/api";
const repository = "a".repeat(32);
const feature = (id: string): FunctionalFeature => ({
  id,
  name: id === "read" ? "Read stock" : "Keep history",
  journey_refs: ["Read stock"],
  acceptance_refs: ["acceptance-1"],
  component_refs: [],
  behaviors: [
    {
      id: "visible",
      given: "Stock exists",
      when: "Read stock",
      then: "Show stock",
      failure_paths: ["Explain unavailability"],
    },
  ],
});
test("incremental contract preview preserves untouched features and resolves only its original saved receipt", async ({
  page,
}, testInfo) => {
  let writes = 0,
    previews = 0;
  let receiptAvailable = false;
  const definition: CustomerProductDefinition = {
    customer: "Fictional stock team",
    problem: "Read stock safely",
    owners: ["Owner"],
    journey: ["Read stock"],
    user_stories: ["Read my stock"],
    acceptance: ["Show available stock"],
    security_requirements: [],
    nonfunctional_requirements: [],
    features: [feature("read"), feature("keep")],
    integrations: [],
    functional_reviewed: true,
    architecture: null,
    design_review: null,
  };
  const base: ProductSnapshot = {
    repository_id: repository,
    revision: 1,
    digest: "1".repeat(64),
    created_at: "2026-10-17T00:00:00Z",
    definition,
  };
  let saved = base;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: repository,
            name: "Fictional stock",
            url: "https://github.com/example/stock",
            provider: "github",
            default_branch: "main",
          },
        ],
      });
    if (path.endsWith("/product/conditional"))
      throw new Error("Incremental actions must not use whole-definition save");
    if (
      path.endsWith("/product/changes/preview") ||
      path.endsWith("/product/changes")
    ) {
      const input = route.request().postDataJSON();
      expect(input.base_revision).toBe(1);
      expect(input.base_digest).toBe(base.digest);
      expect(input.feature_upserts).toHaveLength(1);
      expect(input.feature_upserts[0].id).toBe("read");
      expect(input.remove_feature_ids).toEqual([]);
      const next = {
        ...definition,
        features: [input.feature_upserts[0], feature("keep")],
        functional_reviewed: false,
      };
      const impact = {
        changed_feature_ids: ["read"],
        unchanged_feature_ids: ["keep"],
        unchanged_feature_digests: { keep: "d".repeat(64) },
        changed_integration_ids: [],
        affected_journeys: ["Read stock"],
        regression_acceptance_refs: ["acceptance-1"],
        reviews_required: ["functional", "design"],
      };
      if (path.endsWith("/preview")) {
        previews++;
        return route.fulfill({
          json: {
            base_revision: 1,
            base_digest: base.digest,
            definition: next,
            impact,
            implementation_status: "not_started",
          },
        });
      }
      writes++;
      const canonical = JSON.stringify(input, (_key, item: unknown) =>
        item && typeof item === "object" && !Array.isArray(item)
          ? Object.fromEntries(
              Object.entries(item).sort(([a], [b]) =>
                a < b ? -1 : a > b ? 1 : 0,
              ),
            )
          : item,
      );
      saved = {
        ...base,
        revision: 2,
        digest: "2".repeat(64),
        definition: next,
        change_receipt: {
          request_id: input.request_id,
          request_digest: createHash("sha256").update(canonical).digest("hex"),
          base_revision: 1,
          base_digest: base.digest,
          implementation_status: "not_started",
        },
      };
      return route.abort("connectionreset");
    }
    if (path.endsWith("/product")) {
      if (writes && !receiptAvailable) {
        const withoutReceipt = { ...saved };
        delete withoutReceipt.change_receipt;
        return route.fulfill({ json: withoutReceipt });
      }
      return route.fulfill({ json: saved });
    }
    return route.fulfill({ json: path.endsWith("/brand") ? null : [] });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("test-bootstrap-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Product definition");
  await expect(
    page.getByLabel("Customer or user", { exact: true }),
  ).toHaveValue("Fictional stock team");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Feature name", { exact: true })
    .fill("Read stock with freshness");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByRole("button", { name: "Review definition", exact: true })
    .click();
  await page
    .getByLabel("Why is this changing?", { exact: true })
    .fill("Explain freshness without changing history");
  await page
    .getByRole("button", { name: "Preview contract change", exact: true })
    .click();
  const impact = page.getByRole("region", { name: "Contract change impact" });
  await expect(impact).toContainText("Keep history");
  expect(writes).toBe(0);
  for (const theme of ["light", "dark"] as const) {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.evaluate((theme) => {
      document.documentElement.dataset.theme = theme;
    }, theme);
    const region = page.getByRole("region", {
      name: "Incremental contract change",
    });
    expect(
      await region.evaluate(
        (element) => element.scrollWidth <= element.clientWidth,
      ),
    ).toBe(true);
    const action = page.getByRole("button", {
      name: "Save contract change",
      exact: true,
    });
    await action.scrollIntoViewIfNeeded();
    await action.focus();
    await expect(action).toBeFocused();
    expect(
      await action.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return (
          box.width > 0 &&
          box.left >= 0 &&
          box.right <= innerWidth &&
          element.contains(
            document.elementFromPoint(
              box.x + box.width / 2,
              box.y + box.height / 2,
            ),
          )
        );
      }),
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`product-change-impact-390-${theme}.png`),
    });
  }
  await page
    .getByRole("button", { name: "Save contract change", exact: true })
    .click();
  await expect(
    page.getByText("The save outcome is uncertain.", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Inspect saved definition", exact: true })
    .click();
  await expect(
    page.getByText("The save outcome is uncertain.", { exact: false }),
  ).toBeVisible();
  receiptAvailable = true;
  await navigate(page, "Factory");
  await navigate(page, "Product definition");
  await expect(
    page.getByRole("button", {
      name: "Retry original contract change",
      exact: true,
    }),
  ).toBeVisible();
  expect(writes).toBe(1);
  await page
    .getByRole("button", { name: "Inspect saved definition", exact: true })
    .click();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).toBeVisible();
  expect(writes).toBe(1);
  expect(previews).toBe(1);
  expect(saved.definition.features![1]).toEqual(feature("keep"));
  expect(saved.definition.functional_reviewed).toBe(false);
});
