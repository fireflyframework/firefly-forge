/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { test, expect } from "@playwright/test";
import { navigate, expectAuthenticated } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
const ref = {
  version: 1,
  artifact_id: "a".repeat(32),
  manifest_sha256: "b".repeat(64),
  sha256: "c".repeat(64),
  byte_count: 50_000_000,
  file_count: 20_000,
  index_page_count: 200,
};
const fullPath =
  "backend/warehouse/reservations/migrations/2026/maintain_existing_inventory.py";
const bytes = Buffer.from(
  `diff --git a/${fullPath} b/${fullPath}\n--- a/${fullPath}\n+++ b/${fullPath}\n@@ -1,1 +1,4000 @@\n-old_inventory = None\n+inventory = "世界"\n` +
    "+preserve_existing_reservations = True\n".repeat(4000),
);
const hash = (value: Buffer) =>
  createHash("sha256").update(value).digest("hex");
for (const [width, theme] of [
  [1440, "light"],
  [1440, "dark"],
  [390, "light"],
  [390, "dark"],
] as const) {
  test(`controlled paged diff ${width} ${theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    const run = {
      id: "f".repeat(32),
      repository_id: "e".repeat(32),
      brief: "Review warehouse migration changes",
      status: "failed",
      publish: false,
      diff: "LEGACY_PREVIEW_MUST_NOT_RENDER",
      diff_artifact: ref,
    };
    const calls: string[] = [];
    let corrupt = false,
      denied = false,
      writes = 0;
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      if (
        route.request().method() === "GET" &&
        (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
      )
        return;
      const request = route.request(),
        url = new URL(request.url()),
        path = url.pathname;
      if (request.method() !== "GET") writes++;
      let json: unknown = [];
      if (path.includes("/diff/")) {
        calls.push(url.pathname + url.search);
        expect(url.searchParams.get("expected_manifest")).toBe(
          ref.manifest_sha256,
        );
        if (denied)
          return route.fulfill({
            status: 403,
            json: { detail: "Controlled read scope revoked" },
          });
        if (path.endsWith("/index")) {
          const n = Number(url.searchParams.get("page"));
          json = {
            version: 1,
            artifact_id: ref.artifact_id,
            manifest_sha256: ref.manifest_sha256,
            page: n,
            next_page: n === 0 ? 1 : null,
            files: [
              {
                file_index: n === 0 ? 0 : 100,
                path:
                  n === 0 ? fullPath : "frontend/components/InventoryTable.tsx",
                offset: 0,
                byte_count: bytes.length,
                sha256: hash(bytes),
              },
              {
                file_index: n === 0 ? 1 : 101,
                path: "docs/review/retained-binary-evidence.bin",
                offset: bytes.length,
                byte_count: 4,
                sha256: hash(Buffer.from([255, 0, 128, 1])),
              },
            ],
          };
        } else {
          const index = Number(path.split("/").at(-1)),
            offset = Number(url.searchParams.get("offset"));
          expect(url.searchParams.get("limit")).toBe("32768");
          let content =
            index % 100 === 1
              ? Buffer.from([255, 0, 128, 1])
              : bytes.subarray(offset, offset + 32768);
          if (index % 100 !== 1) {
            while (content.length) {
              try {
                new TextDecoder("utf-8", { fatal: true }).decode(content);
                break;
              } catch {
                content = content.subarray(0, -1);
              }
            }
          }
          const total = index % 100 === 1 ? 4 : bytes.length;
          json = {
            version: 1,
            artifact_id: ref.artifact_id,
            manifest_sha256: ref.manifest_sha256,
            file_index: index,
            offset,
            byte_count: content.length,
            total_bytes: total,
            next_offset:
              offset + content.length < total ? offset + content.length : null,
            content_base64: content.toString("base64"),
            content_sha256: corrupt ? "0".repeat(64) : hash(content),
            starts_mid_line: offset > 0,
            ends_mid_line: offset + content.length < total,
            utf8_complete: index % 100 !== 1,
          };
        }
      } else if (path.endsWith("/repositories"))
        json = [
          {
            id: run.repository_id,
            name: "Warehouse",
            url: "https://example.test/warehouse.git",
            provider: "git",
            default_branch: "main",
          },
        ];
      else if (path.endsWith("/runs")) json = [run];
      else if (path.endsWith("/runs/" + run.id)) json = run;
      else if (path.endsWith("/health"))
        json = { status: "ok", execution_ready: false };
      else if (path.endsWith("/journal")) json = { valid: true, entries: [] };
      else if (path.endsWith("/plan")) json = { revision: 1, tasks: [] };
      return route.fulfill({ json });
    });
    await page.goto("/");
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("controlled-diff-viewer-token-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await expectAuthenticated(page);
    await navigate(page, "Factory");
    await openFactoryRun(page, /Review warehouse migration changes/);
    await page.getByRole("tab", { name: "Changes", exact: true }).click();
    const panel = page.getByRole("region", {
      name: "Saved source changes",
      exact: true,
    });
    await expect(
      panel.getByText("Page checksum verified", { exact: true }),
    ).toBeVisible();
    await expect(panel).toContainText("20,000 changed files");
    await expect(panel).toContainText("Partial file window.");
    await expect(panel).not.toContainText("LEGACY_PREVIEW_MUST_NOT_RENDER");
    expect(
      calls.every(
        (path) => path.includes("/index?") || path.includes("/files/0?"),
      ),
    ).toBe(true);
    await panel.scrollIntoViewIfNeeded();
    const geometry = await panel.evaluate((el) => ({
      width: el.getBoundingClientRect().width,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      targets: [...el.querySelectorAll("button")]
        .filter((x) => x.getClientRects().length > 0)
        .map((x) => x.getBoundingClientRect().height),
    }));
    expect(geometry.overflow).toBe(false);
    expect(geometry.width).toBeLessThanOrEqual(width);
    expect(geometry.targets.every((height) => height >= 44)).toBe(true);
    await page.screenshot({
      path: info.outputPath(`diff-${width}-${theme}.png`),
      fullPage: false,
    });
    await panel
      .getByRole("button", { name: "Next window", exact: true })
      .click();
    await expect(panel).toContainText("Bytes 32,769");
    await panel
      .getByRole("button", { name: "Previous window", exact: true })
      .click();
    await expect(panel).toContainText("Bytes 1–");
    if (width === 390)
      await panel
        .getByRole("button", { name: "Browse files", exact: true })
        .click();
    await panel
      .getByRole("button", { name: /docs\/review\/retained-binary-evidence/ })
      .click();
    await expect(panel).toContainText("Exact bytes are shown as hexadecimal");
    await expect(panel.getByLabel("Exact diff bytes")).toHaveText(
      "ff 00 80 01",
    );
    corrupt = true;
    if (width === 390)
      await panel
        .getByRole("button", { name: "Browse files", exact: true })
        .click();
    await panel
      .getByRole("button", {
        name: new RegExp("^" + fullPath.replaceAll(".", "\\.")),
      })
      .click();
    await expect(panel.getByRole("alert")).toContainText(
      "No code from this response is displayed",
    );
    await expect(panel.getByLabel("Git diff", { exact: true })).toHaveCount(0);
    denied = true;
    await panel
      .getByRole("button", { name: "Refresh index", exact: true })
      .click();
    await expect(panel.getByRole("alert")).toContainText(
      "file index could not be read",
    );
    expect(writes).toBe(0);
  });
}
