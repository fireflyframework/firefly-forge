/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import {
  mockFactoryDashboard,
  openFactoryRun,
} from "./factory-dashboard-fixture";
import { navigate } from "./navigation";

const repository = resolve(import.meta.dirname, "../..");
const git = (...args: string[]) =>
  execFileSync("git", ["-C", repository, ...args]);
const sha = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const origin = {
  repository: "fireflyframework/firefly-forge",
  commit: git("rev-parse", "HEAD").toString().trim(),
  tree: git("rev-parse", "HEAD^{tree}").toString().trim(),
  branch: git("branch", "--show-current").toString().trim() || null,
  controlled_api: true,
  live_archive_acceptance: false,
};
const files = [
  "src/firefly_forge/engine_activity.py",
  "studio/src/RepositoryLabels.ts",
  "studio/src/SourceExplorerData.ts",
].map((path) => ({ path, bytes: git("show", `${origin.commit}:${path}`) }));
const entries = files.map((file) => ({
  path: file.path,
  kind: "file",
  mode: 33188,
  byte_count: file.bytes.length,
  sha256: sha(file.bytes),
}));
const digest = sha(Buffer.from(JSON.stringify(entries)));
const reference = {
  protocol: "forge-source-archive-v1",
  snapshot_id: "b".repeat(32),
  tenant_id: "1".repeat(32),
  project_id: "2".repeat(32),
  repository_id: "3".repeat(32),
  run_id: "a".repeat(32),
  attempt: 1,
  head: origin.commit,
  tree: origin.tree,
  branch: origin.branch,
  storage_binding: "9".repeat(64),
  catalog: {
    version: 1,
    artifact_id: "c".repeat(32),
    manifest_sha256: digest,
    source_digest: digest,
    manifest_digest: digest,
    byte_count: files.reduce((sum, file) => sum + file.bytes.length, 0),
    file_count: files.length,
    entry_count: entries.length,
    page_count: 1,
  },
};
const run = {
  id: reference.run_id,
  repository_id: reference.repository_id,
  project_id: reference.project_id,
  brief: "Explore recorded source",
  status: "succeeded",
  operation: "test",
  agent: "api",
  backend: "python",
  frontend: "react",
  events: [],
  verification: [],
};
for (const width of [1440, 390])
  for (const theme of ["light", "dark"] as const) {
    test(`repository source explorer ${width} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      await page.addInitScript(() =>
        Object.defineProperty(navigator, "clipboard", {
          value: {
            writeText: async (text: string) => {
              Reflect.set(window, "sourceClipboard", text);
            },
          },
        }),
      );
      let writes = 0,
        external = 0;
      await page.route("**/*", async (route) => {
        const request = route.request(),
          url = new URL(request.url());
        if (url.hostname !== "127.0.0.1") {
          external++;
          return route.abort();
        }
        if (await mockIdentity(route)) return;
        if (request.method() !== "GET") {
          writes++;
          return route.abort();
        }
        if (!url.pathname.startsWith("/api/v1/")) return route.continue();
        if (await mockFactoryDashboard(route, { projects: [], runs: [run] }))
          return;
        if (url.pathname.endsWith("/source/revisions"))
          return route.fulfill({
            json: {
              run_id: run.id,
              items: [{ reference, status: "published", reason: null }],
              next_after: null,
              recording_enabled: true,
              availability: "recorded",
            },
          });
        if (url.pathname.includes("/source/revisions/")) {
          expect(url.searchParams.get("expected_manifest")).toBe(digest);
          expect(url.searchParams.get("page")).toBe("0");
          if (url.pathname.endsWith("/catalog"))
            return route.fulfill({
              json: {
                reference,
                catalog: {
                  version: 1,
                  artifact_id: reference.catalog.artifact_id,
                  manifest_sha256: digest,
                  page: 0,
                  next_page: null,
                  entries,
                },
              },
            });
          const index = Number(url.pathname.split("/").at(-1)),
            offset = Number(url.searchParams.get("offset")),
            limit = Number(url.searchParams.get("limit"));
          const file = files[index],
            bytes = file.bytes.subarray(offset, offset + limit);
          return route.fulfill({
            json: {
              reference,
              entry: entries[index],
              catalog_page: 0,
              entry_index: index,
              offset,
              byte_count: bytes.length,
              next_offset:
                offset + bytes.length < file.bytes.length
                  ? offset + bytes.length
                  : null,
              sha256: sha(bytes),
              content_base64: bytes.toString("base64"),
              binary: false,
              text: bytes.toString("utf8"),
            },
          });
        }
        return route.fulfill({
          json: url.pathname.endsWith("/repositories")
            ? [
                {
                  id: run.repository_id,
                  name: "firefly-forge",
                  url: "https://github.com/fireflyframework/firefly-forge.git",
                  provider: "github",
                  default_branch: "main",
                },
              ]
            : url.pathname.endsWith("/runs")
              ? [run]
              : url.pathname.endsWith("/runs/" + run.id)
                ? run
                : url.pathname.endsWith("/health")
                  ? { status: "ok", execution_ready: true }
                  : [],
        });
      });
      await page.goto("/");
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("controlled-source-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await navigate(page, "Factory");
      await openFactoryRun(page, run.brief);
      await page.getByRole("tab", { name: "Code", exact: true }).click();
      const panel = page.getByRole("region", {
        name: "Recorded source snapshot",
        exact: true,
      });
      const code = panel.getByLabel("Recorded source code");
      await expect(code.locator(".hljs-keyword").first()).toBeVisible();
      expect(await code.locator("code").textContent()).toBe(
        files[0].bytes.toString("utf8"),
      );
      await expect(code.locator(".code-line-numbers")).toBeVisible();
      await expect(
        panel.getByText("Technical details", { exact: true }),
      ).toBeVisible();
      await expect(
        panel.getByText("Window checksum verified", { exact: true }),
      ).not.toBeVisible();
      const openingHeight = await code.evaluate((element) => {
        const box = element.getBoundingClientRect();
        const main = document
          .querySelector(".workspace-main > main")!
          .getBoundingClientRect();
        return Math.min(box.bottom, main.bottom) - Math.max(box.top, main.top);
      });
      expect(openingHeight).toBeGreaterThan(200);
      await expect(
        page.getByRole("tab", { name: "Code", exact: true }),
      ).toBeFocused();
      await page.screenshot({
        path: info.outputPath(`run-detail-code-opening-${width}-${theme}.png`),
      });
      if (width === 1440 && theme === "light") {
        await page.locator(".workspace-main > main").evaluate((element) => {
          element.scrollTop = 0;
        });
        await expect(
          page.getByRole("heading", {
            level: 1,
            name: "Verify existing behavior",
            exact: true,
          }),
        ).toBeInViewport();
        await expect(
          page.getByRole("tab", { name: "Code", exact: true }),
        ).toBeInViewport();
      }
      await panel.evaluate((element) =>
        element.scrollIntoView({ block: "start" }),
      );
      await page.evaluate(() => document.fonts.ready);
      const geometry = await code.evaluate((element) => {
        const rect = element.getBoundingClientRect(),
          main = document
            .querySelector(".workspace-main > main")!
            .getBoundingClientRect();
        return {
          codeTop: rect.top,
          visibleCodeHeight:
            Math.min(rect.bottom, main.bottom) - Math.max(rect.top, main.top),
          horizontalPageOverflow:
            document.documentElement.scrollWidth > innerWidth + 1,
        };
      });
      expect(geometry.visibleCodeHeight).toBeGreaterThan(200);
      expect(geometry.codeTop).toBeLessThan(540);
      expect(geometry.horizontalPageOverflow).toBe(false);
      await info.attach("code-geometry", {
        body: JSON.stringify(geometry),
        contentType: "application/json",
      });
      await info.attach("source-origin", {
        body: JSON.stringify({ ...origin, files: entries }),
        contentType: "application/json",
      });
      await page.screenshot({
        path: info.outputPath(`repository-source-${width}-${theme}.png`),
      });
      await panel
        .getByRole("button", { name: "Copy file", exact: true })
        .click();
      expect(
        await page.evaluate(() => Reflect.get(window, "sourceClipboard")),
      ).toBe(files[0].bytes.toString("utf8"));
      await expect(
        panel.getByRole("navigation", {
          name: "Source catalog pages",
          exact: true,
        }),
      ).toHaveCount(0);
      await code.focus();
      const mainScroll = await page
        .locator(".workspace-main > main")
        .evaluate((element) => element.scrollTop);
      await page.keyboard.press("PageDown");
      await expect
        .poll(() => code.evaluate((element) => element.scrollTop))
        .toBeGreaterThan(0);
      await page.keyboard.press("ArrowRight");
      await expect
        .poll(() => code.evaluate((element) => element.scrollLeft))
        .toBeGreaterThan(0);
      expect(
        await page
          .locator(".workspace-main > main")
          .evaluate((element) => element.scrollTop),
      ).toBe(mainScroll);

      if (width === 390)
        await panel
          .getByRole("button", { name: "Browse files", exact: true })
          .click();
      const next = panel.getByRole("button", {
        name: "studio/src/RepositoryLabels.ts",
        exact: true,
      });
      const folder = panel.getByRole("button", {
        name: "studio/",
        exact: true,
      });
      await folder.focus();
      await page.keyboard.press("Space");
      await expect(folder).toHaveAttribute("aria-expanded", "false");
      await expect(next).not.toBeVisible();
      await page.keyboard.press("Enter");
      await expect(folder).toHaveAttribute("aria-expanded", "true");

      await next.focus();
      await page.keyboard.press("Enter");
      await expect(code.locator("code")).toHaveText(
        files[1].bytes.toString("utf8"),
      );
      expect(await code.locator("code").textContent()).toBe(
        files[1].bytes.toString("utf8"),
      );
      await expect(code.locator(".hljs-keyword").first()).toBeVisible();
      await panel
        .getByRole("button", { name: "Copy path", exact: true })
        .click();
      expect(
        await page.evaluate(() => Reflect.get(window, "sourceClipboard")),
      ).toBe(files[1].path);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth + 1,
        ),
      ).toBe(false);
      expect(writes).toBe(0);
      expect(external).toBe(0);
    });
  }
