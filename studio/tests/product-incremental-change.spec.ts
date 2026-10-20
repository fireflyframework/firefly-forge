/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Locator } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import type {
  CustomerProductDefinition,
  FunctionalFeature,
  ProductSnapshot,
} from "../src/api";
const repository = "a".repeat(32);
const layouts = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
  { width: 320, height: 400 },
].flatMap((viewport) =>
  ["light", "dark"].map((theme) => ({ ...viewport, theme })),
);
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
    previews = 0,
    insertions = 0;
  let receiptAvailable = false,
    originalBody: string | undefined;
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
  let saved = base,
    original = base;
  async function reachable(control: Locator) {
    await control.scrollIntoViewIfNeeded();
    await control.focus();
    await expect(control).toBeFocused();
    await expect
      .poll(() =>
        control.evaluate((element) => {
          const box = element.getBoundingClientRect(),
            header = document
              .querySelector(".workspace-main > header")!
              .getBoundingClientRect(),
            footer = document
              .querySelector(".workspace-main > footer")!
              .getBoundingClientRect();
          return (
            box.width > 0 &&
            box.left >= 0 &&
            box.right <= innerWidth &&
            box.top >= header.bottom &&
            box.bottom <= footer.top &&
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
  async function captureLayout(
    stage: string,
    region: Locator,
    controls: Locator[],
    anchor: Locator,
    views = layouts,
  ) {
    for (const view of views) {
      await page.setViewportSize({ width: view.width, height: view.height });
      await page
        .getByLabel("Appearance", { exact: true })
        .selectOption(view.theme);
      await expect(page.locator("html")).toHaveAttribute(
        "data-theme",
        view.theme,
      );
      await expect(region).toBeVisible();
      const geometry = await region.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const groups = [
          element,
          ...element.querySelectorAll(".wizard-summary"),
        ];
        const overlaps = groups.some((group) => {
          const rows = [...group.children]
            .map((node) => node.getBoundingClientRect())
            .filter((rect) => rect.width && rect.height);
          return rows.some((a, index) =>
            rows
              .slice(index + 1)
              .some(
                (b) =>
                  a.left < b.right - 1 &&
                  a.right > b.left + 1 &&
                  a.top < b.bottom - 1 &&
                  a.bottom > b.top + 1,
              ),
          );
        });
        return {
          fits: box.left >= 0 && box.right <= innerWidth,
          overflow: element.scrollWidth > element.clientWidth + 1,
          overlaps,
        };
      });
      expect(geometry).toEqual({
        fits: true,
        overflow: false,
        overlaps: false,
      });
      const mainGeometry = await page.locator("main").evaluate((element) => {
        const fits = element.scrollWidth <= element.clientWidth + 1;
        if (fits) return { fits };
        const describe = (node: Element) => {
          const box = node.getBoundingClientRect(),
            style = getComputedStyle(node);
          return {
            tag: node.tagName,
            className: String(node.className),
            x: box.x,
            right: box.right,
            width: box.width,
            clientWidth: node.clientWidth,
            scrollWidth: node.scrollWidth,
            minWidth: style.minWidth,
            maxWidth: style.maxWidth,
            whiteSpace: style.whiteSpace,
            overflowX: style.overflowX,
            display: style.display,
            flexWrap: style.flexWrap,
            gap: style.gap,
            paddingLeft: style.paddingLeft,
            paddingRight: style.paddingRight,
            font: style.font,
          };
        };
        const boundary =
          element.getBoundingClientRect().left + element.clientWidth;
        return {
          fits,
          viewport: { width: innerWidth, height: innerHeight },
          fonts: document.fonts.status,
          manropeLoaded: document.fonts.check("650 13px Manrope"),
          main: describe(element),
          offenders: [...element.querySelectorAll("*")]
            .filter((node) => {
              const box = node.getBoundingClientRect();
              return (
                box.width > 0 &&
                (box.right > boundary + 1 ||
                  node.scrollWidth > node.clientWidth + 1)
              );
            })
            .slice(0, 28)
            .map(describe),
        };
      });
      expect(
        mainGeometry.fits,
        JSON.stringify({ stage, ...mainGeometry }),
      ).toBe(true);
      for (const control of controls) await reachable(control);
      await anchor.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: testInfo.outputPath(
          `product-change-${stage}-${view.width}x${view.height}-${view.theme}.png`,
        ),
      });
    }
  }
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
      if (originalBody) {
        expect(route.request().postData()).toBe(originalBody);
        return route.fulfill({ json: original });
      }
      originalBody = route.request().postData()!;
      insertions++;
      const canonical = JSON.stringify(input, (_key, item: unknown) =>
        item && typeof item === "object" && !Array.isArray(item)
          ? Object.fromEntries(
              Object.entries(item).sort(([a], [b]) =>
                a < b ? -1 : a > b ? 1 : 0,
              ),
            )
          : item,
      );
      const requestDigest = Array.from(
        new Uint8Array(
          await crypto.subtle.digest(
            "SHA-256",
            new TextEncoder().encode(canonical),
          ),
        ),
      )
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("");
      original = {
        ...base,
        revision: 2,
        digest: "2".repeat(64),
        definition: next,
        change_receipt: {
          request_id: input.request_id,
          request_digest: requestDigest,
          base_revision: 1,
          base_digest: base.digest,
          implementation_status: "not_started",
        },
      };
      saved = original;
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
  const change = page.getByRole("region", {
    name: "Incremental contract change",
    exact: true,
  });
  const reason = page.getByLabel("Why is this changing?", { exact: true });
  const previewAction = page.getByRole("button", {
    name: "Preview contract change",
    exact: true,
  });
  const saveAction = page.getByRole("button", {
    name: "Save contract change",
    exact: true,
  });
  await reason.fill("Explain freshness without changing history");
  await captureLayout(
    "reason",
    change,
    [reason, previewAction],
    reason.locator(".."),
  );
  await expect(reason).toHaveValue(
    "Explain freshness without changing history",
  );
  expect(previews).toBe(0);
  expect(writes).toBe(0);
  await previewAction.click();
  const impact = page.getByRole("region", {
    name: "Contract change impact",
    exact: true,
  });
  await expect(impact).toContainText("Keep history");
  expect(writes).toBe(0);
  await captureLayout(
    "impact",
    change,
    [previewAction, saveAction],
    impact.locator(".wizard-summary > div").first(),
  );
  // A short viewport needs an action frame; the impact remains normally scrollable.
  await captureLayout(
    "impact-action",
    change,
    [saveAction],
    saveAction,
    layouts.filter((view) => view.height === 400),
  );
  await saveAction.click();
  await expect(
    page.getByText("The save outcome is uncertain.", { exact: false }),
  ).toBeVisible();
  const recovery = page.locator(".product-save-recovery");
  const inspect = page.getByRole("button", {
    name: "Inspect saved definition",
    exact: true,
  });
  const retry = page.getByRole("button", {
    name: "Retry original contract change",
    exact: true,
  });
  const discard = page.getByRole("button", {
    name: "Discard edits",
    exact: true,
  });
  const fullSave = page.getByRole("button", {
    name: "Save draft definition",
    exact: true,
  });
  await expect(reason).toBeDisabled();
  await expect(previewAction).toBeDisabled();
  await expect(fullSave).toBeDisabled();
  await expect(discard).toBeDisabled();
  await captureLayout(
    "held",
    recovery,
    [inspect, retry],
    recovery.locator("p"),
  );
  expect(writes).toBe(1);
  expect(insertions).toBe(1);
  await inspect.click();
  await expect(recovery).toBeVisible();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).not.toBeVisible();
  expect(writes).toBe(1);

  // A later writer advances the product while the original receipt stays immutable.
  receiptAvailable = true;
  saved = {
    ...original,
    revision: 3,
    digest: "3".repeat(64),
    definition: {
      ...original.definition,
      customer: "Another product owner",
      features: [
        { ...feature("read"), name: "Read stock for another owner" },
        feature("keep"),
      ],
    },
    change_receipt: {
      request_id: "d".repeat(32),
      request_digest: "d".repeat(64),
      base_revision: 2,
      base_digest: original.digest,
      implementation_status: "not_started",
    },
  };
  await navigate(page, "Factory");
  await navigate(page, "Product definition");
  await expect(retry).toBeVisible();
  await inspect.click();
  await expect(recovery).toBeVisible();
  await expect(discard).toBeDisabled();
  await expect(fullSave).toBeDisabled();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).not.toBeVisible();
  expect(writes).toBe(1);

  // Explicit identical replay settles the old receipt and must not replace revision3.
  await retry.click();
  await expect(recovery).not.toBeVisible();
  const stale = page.getByRole("alert").filter({
    hasText: "The saved definition changed since this draft was opened.",
  });
  await expect(stale).toBeVisible();
  await expect(discard).toBeEnabled();
  await expect(fullSave).toBeDisabled();
  await expect(previewAction).toBeDisabled();
  await expect(
    page
      .locator(".product-document")
      .getByText("Fictional stock team", { exact: true }),
  ).toBeVisible();
  await expect(
    page
      .locator(".product-document")
      .getByText("Read stock with freshness", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Product definition saved.", { exact: true }),
  ).not.toBeVisible();
  await captureLayout(
    "original-receipt-settled-newer-held",
    stale,
    [discard],
    stale,
    layouts.filter((view) => view.width === 320 && view.theme === "dark"),
  );
  expect(writes).toBe(2);
  expect(insertions).toBe(1);
  expect(saved.revision).toBe(3);
  expect(saved.definition.customer).toBe("Another product owner");

  await discard.click();
  const confirmation = page.getByRole("dialog", {
    name: "Discard product edits",
    exact: true,
  });
  await expect(confirmation).toBeVisible();
  await confirmation
    .getByRole("button", { name: "Keep editing", exact: true })
    .click();
  await expect(stale).toBeVisible();
  expect(writes).toBe(2);
  await discard.click();
  await confirmation
    .getByRole("button", { name: "Discard this draft", exact: true })
    .click();
  const currentCustomer = page.getByLabel("Customer or user", { exact: true });
  await expect(currentCustomer).toHaveValue("Another product owner");
  await expect(page.locator(".product-origin")).toContainText(
    "Saved revision 3",
  );
  await expect(recovery).not.toBeVisible();
  await expect(stale).not.toBeVisible();
  await captureLayout(
    "current-revision",
    page.locator(".product-document"),
    [
      currentCustomer,
      page.getByRole("button", { name: "Continue", exact: true }),
    ],
    currentCustomer.locator(".."),
  );
  expect(writes).toBe(2);
  expect(insertions).toBe(1);
  expect(previews).toBe(1);
  expect(original.definition.features![1]).toEqual(feature("keep"));
  expect(original.definition.functional_reviewed).toBe(false);
});
