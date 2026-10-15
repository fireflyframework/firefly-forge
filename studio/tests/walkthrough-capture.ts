/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import { mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve, relative } from "node:path";
import { fileURLToPath } from "node:url";

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
export async function captureWalkthrough(
  page: Page,
  stage: string,
  theme: string,
  device: string,
  options: {
    producer?: string;
    resetFields?: boolean;
    actionSelector?: string;
  } = {},
) {
  await page.evaluate(() => document.fonts.ready);
  const currentVersion = JSON.parse(
    readFileSync(resolve(root, "studio/package.json"), "utf8"),
  ).version;
  await expect(page.locator(".workspace-main > footer")).toContainText(
    "v" + currentVersion,
  );
  if (
    (options.resetFields ?? true) &&
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
    "search-provider": "Provider",
    "search-access": "Access",
    "search-review": "Review",
    "learning-knowledge": "Knowledge",
    "learning-limits": "Limits",
    "learning-review": "Review",
    "learning-invalidation-reason": "Reason",
    "learning-invalidation-review": "Review",
    "learning-observation-source": "Source",
    "learning-observation-review": "Review",
    "campaign-report-save-review": "Review",
    "runner-continuation-selection": "Original command",
    "runner-continuation-review": "Review",
    "source-analysis-scope": "Scope",
    "source-analysis-receipt": "Receipt",
    "source-analysis-evidence": "Evidence",
    "source-analysis-review": "Review",
  };
  if (expectedStep[stage])
    await expect(
      page.locator('.wizard-steps [aria-current="step"]'),
    ).toContainText(expectedStep[stage]);
  const action = page
    .locator(
      options.actionSelector ??
        ".wizard-actions button.primary, .search-actions button.secondary",
    )
    .last();
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
  const producer = options.producer ?? "studio/tests/docs-walkthrough.spec.ts";
  const fingerprint = (value: Record<string, string>) =>
    digest(
      JSON.stringify(
        Object.fromEntries(
          Object.entries(value).sort(([a], [b]) =>
            a < b ? -1 : a > b ? 1 : 0,
          ),
        ),
      ),
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
  const hashInputs = (paths: string[]) =>
    Object.fromEntries(
      paths.map((path) => [path, digest(readFileSync(resolve(root, path)))]),
    );
  const helpers = hashInputs([
    "studio/tests/walkthrough-capture.ts",
    "studio/tests/navigation.ts",
    "studio/tests/auth-fixture.ts",
    "studio/tests/learning-record-fixture.ts",
    "studio/tests/campaign-report-fixture.ts",
    "studio/tests/campaign-report-history-fixture.ts",
    "studio/tests/campaign-report-archive-fixture.json",
  ]);
  const producers = hashInputs([
    "studio/tests/docs-walkthrough.spec.ts",
    "studio/tests/external-tools.spec.ts",
  ]);
  if (!(producer in producers)) throw new Error("Unknown capture producer");
  const config = {
    path: "studio/playwright.docs.config.ts",
    sha256: digest(
      readFileSync(resolve(root, "studio/playwright.docs.config.ts")),
    ),
  };
  for (const record of Object.values(manifest.captures) as Record<
    string,
    unknown
  >[]) {
    record.studio_version ??= manifest.studio_version;
    record.frontend_digest ??= fingerprint(manifest.frontend);
    if (!record.producer) {
      record.producer = "studio/tests/docs-walkthrough.spec.ts";
      record.producer_sha256 =
        record.capture_test_sha256 ?? manifest.capture_test_sha256;
      record.capture_helpers_digest = "legacy-schema-1";
      record.capture_config_sha256 = "legacy-schema-1";
    }
  }
  manifest.captures[name] = {
    producer,
    producer_sha256: producers[producer],
    capture_helpers_digest: fingerprint(helpers),
    capture_config_sha256: config.sha256,
    studio_version: currentVersion,
    frontend_digest: fingerprint(currentSource),
    path: `assets/screenshots/${name}`,
    stage,
    theme,
    device,
    fixture: true,
    viewport: page.viewportSize(),
    sha256: digest(png),
    action: control,
    captured_action: capturedAction,
  };
  manifest.schema_version = 2;
  manifest.fixture_only = true;
  manifest.studio_version = currentVersion;
  manifest.frontend = currentSource;
  manifest.capture_helpers = helpers;
  manifest.capture_producers = producers;
  manifest.capture_config = config;
  delete manifest.capture_test_sha256;
  writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");
}
