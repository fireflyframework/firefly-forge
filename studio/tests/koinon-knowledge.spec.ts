/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled HTTP fixtures exercise UI only; they do not qualify a live Koinon service.
import { test, expect } from "@playwright/test";
const repository = "a".repeat(32);
for (const width of [1440, 390])
  for (const theme of ["light", "dark"]) {
    test(`native Koinon mapping, access and cited search ${width} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      let configuration = {
        provider: "koinon",
        version: 2,
        enabled: true,
        required: true,
        auto_memory: true,
        endpoint: "https://knowledge.example/api/v1",
        tenant_id: "tenant",
        top_k: 25,
        ca_certificate: "",
        access_token_configured: true,
        project_scopes: {} as Record<string, unknown>,
        framework_scope: null,
        readiness: {
          state: "configured",
          detail: "Controlled fixture; mapped access has not been checked.",
        },
      };
      let saves = 0,
        checks = 0,
        searches = 0,
        deny = false;
      await page.route("**/api/v1/**", async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/knowledge/configuration"))
          return route.fulfill({ json: configuration });
        if (path.endsWith("/knowledge/koinon/configuration")) {
          saves++;
          const body = route.request().postDataJSON();
          expect(body.settings.project_scopes[repository]).toEqual({
            workspace_id: "workspace",
            project_id: "project",
            collection_id: "collection",
            dataset_ids: ["dataset"],
          });
          expect(body.settings.access_token).toBe("");
          configuration = {
            ...configuration,
            ...body.settings,
            access_token_configured: true,
          };
          return route.fulfill({ json: configuration });
        }
        if (path.endsWith("/knowledge/test")) {
          checks++;
          expect(route.request().postDataJSON()).toEqual({
            repository_id: repository,
          });
          if (deny)
            return route.fulfill({
              status: 403,
              json: { detail: "Mapped scope access was revoked." },
            });
          configuration.readiness = {
            state: "connected",
            detail:
              "Mapped Koinon catalog authentication succeeded; retrieval needs separate evidence.",
          };
          return route.fulfill({ json: configuration });
        }
        if (path.endsWith("/knowledge/search")) {
          searches++;
          return route.fulfill({
            json: {
              warnings: [],
              hits: [
                {
                  title: "Controlled reservation reference",
                  workspace_id: "workspace",
                  source_version_id: null,
                  provenance: "not_recorded",
                  content:
                    "The fixture retains inventory reservations before allocating stock.",
                },
              ],
            },
          });
        }
        throw new Error("Unadmitted fixture API path " + path);
      });
      await page.route(
        (url) => url.pathname === "/",
        async (route) => {
          const response = await route.fetch();
          const html = await response.text();
          expect(html).toContain(
            '<script type="module" src="/src/main.tsx"></script>',
          );
          return route.fulfill({
            response,
            body: html.replace(
              "/src/main.tsx",
              "/tests/koinon-renderer-fixture.ts",
            ),
          });
        },
      );
      await page.goto(`/?theme=${theme}`);
      await expect(
        page.getByRole("heading", { name: "koinon knowledge connection" }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Check mapped access" }),
      ).toBeDisabled();
      await page.getByLabel("Repository mapping").selectOption(repository);
      await page
        .getByLabel("Workspace catalog ID", { exact: true })
        .fill("workspace");
      await page
        .getByLabel("Project catalog ID", { exact: true })
        .fill("project");
      await page
        .getByLabel("Collection catalog ID (optional)", { exact: true })
        .fill("collection");
      await page.getByLabel("Dataset catalog IDs (optional)").fill("dataset");
      await page.getByRole("button", { name: "Save Koinon mapping" }).click();
      await expect(page.getByRole("status")).toContainText("mapping saved");
      await page.getByRole("button", { name: "Check mapped access" }).click();
      await expect(page.getByRole("status")).toContainText(
        "retrieval needs separate evidence",
      );
      await page
        .getByLabel("Search project", { exact: true })
        .selectOption(repository);
      await page
        .getByLabel("Search question")
        .fill("How are reservations retained?");
      await page
        .getByRole("button", { name: "Search project knowledge" })
        .click();
      await expect(
        page.getByText("Controlled reservation reference", { exact: true }),
      ).toBeVisible();
      await expect(page.getByText(/Source version not recorded/)).toBeVisible();
      expect(saves).toBe(1);
      expect(checks).toBe(1);
      expect(searches).toBe(1);
      await page
        .getByRole("heading", { name: "koinon knowledge connection" })
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`koinon-${width}-${theme}-mapping.png`),
        fullPage: true,
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      deny = true;
      await page.getByRole("button", { name: "Check mapped access" }).click();
      await expect(page.getByRole("alert")).toContainText("access was revoked");
      await page.getByRole("alert").scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`koinon-${width}-${theme}-revoked.png`),
        fullPage: true,
      });
      expect(errors).toEqual([]);
    });
  }
