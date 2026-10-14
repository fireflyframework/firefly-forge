/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { record } from "./learning-record-fixture";
const repository = "a".repeat(32);
async function open(
  page: Page,
  options: {
    records?: unknown[];
    denied?: boolean;
    projection?: unknown;
    projectionDenied?: boolean;
    access?: { actor: string; denied: boolean };
    alternate?: boolean;
  } = {},
) {
  const calls: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request(),
      path = new URL(request.url()).pathname;
    calls.push(request.method() + " " + path);
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({ json: { mode: "local_bootstrap" } });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: options.access?.actor ?? "f".repeat(32),
            display_name: "Owned viewer",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: false,
          memberships: [],
          roles: {},
        },
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: repository,
            name: "Owned inventory",
            url: "/owned/inventory",
            provider: "local",
            default_branch: "main",
          },
          ...(options.alternate
            ? [
                {
                  id: "9".repeat(32),
                  name: "Owned empty inventory",
                  url: "/owned/empty-inventory",
                  provider: "local",
                  default_branch: "main",
                },
              ]
            : []),
        ],
      });
    if (path.endsWith("/knowledge/configuration"))
      return route.fulfill({
        status: 403,
        json: { detail: "Factory administrator access required" },
      });
    if (path.endsWith("/" + repository + "/learning"))
      return route.fulfill({
        status: options.denied || options.access?.denied ? 403 : 200,
        json:
          options.denied || options.access?.denied
            ? { detail: "Project read permission denied" }
            : (options.records ?? []),
      });
    if (path.endsWith("/projection"))
      return route.fulfill({
        status: options.projectionDenied ? 403 : 200,
        json: options.projectionDenied
          ? { detail: "Projection read permission denied" }
          : (options.projection ?? {
              id: "1".repeat(32),
              record_id: record.id,
              state: "unknown",
              workspace_id: "owned-learning-workspace",
              content_sha256: "2".repeat(64),
              request_id: "3".repeat(32),
            }),
      });
    return route.fulfill({
      json: [
        "/runs",
        "/projects",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
        "/learning",
      ].some((suffix) => path.endsWith(suffix))
        ? []
        : {},
    });
  });
  await page.goto("/");
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Learning records", exact: true })
    .click();
  return calls;
}
test("an identity change clears prior evidence and reads the new actor's authorized scope", async ({
  page,
}) => {
  const access = { actor: "f".repeat(32), denied: false };
  await open(page, { records: [record], access });
  await page
    .getByRole("button", { name: "Verify inventory reservation", exact: true })
    .click();
  await expect(page.locator("main")).toContainText(record.envelope.statement);
  access.actor = "9".repeat(32);
  access.denied = true;
  await page.evaluate(() =>
    window.dispatchEvent(new Event("forge-session-expired")),
  );
  await expect(page.getByRole("alert")).toContainText(
    "Project read permission denied",
  );
  await expect(
    page.getByText(record.envelope.statement, { exact: true }),
  ).toHaveCount(0);
});
test("authorized local evidence stays readable without factory knowledge configuration permission", async ({
  page,
}) => {
  const calls = await open(page);
  await expect(
    page.getByRole("heading", { name: "Learning records", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("No learning records returned for this repository.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(calls).toContain(
    "GET /api/v1/repositories/" + repository + "/learning",
  );
  expect(calls.filter((call) => !call.startsWith("GET "))).toEqual([]);
});
test("saved verification and unknown projection remain distinct from live reuse eligibility", async ({
  page,
}) => {
  const calls = await open(page, { records: [record] });
  await page
    .getByRole("button", { name: "Verify inventory reservation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Verify inventory reservation",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator("main")).toContainText(
    "The reservation regression passed against the inspected source.",
  );
  await expect(page.locator("main")).toContainText(
    "Reuse is checked during lookup",
  );
  await expect(page.locator("main")).toContainText("Outcome unknown");
  await expect(
    page.getByRole("button", { name: /submit|invalidate|observe/i }),
  ).toHaveCount(0);
  expect(calls).toContain(
    "GET /api/v1/learning/records/" + record.id + "/projection",
  );
  expect(calls.filter((call) => !call.startsWith("GET "))).toEqual([]);
});
test("denied list access shows server denial without claiming an empty authorized repository", async ({
  page,
}) => {
  await open(page, { denied: true });
  await expect(page.getByRole("alert")).toContainText(
    "Project read permission denied",
  );
  await expect(
    page.getByText("No learning records returned for this repository.", {
      exact: true,
    }),
  ).toHaveCount(0);
});
test("foreign repository response is rejected rather than displaying its statement", async ({
  page,
}) => {
  await open(page, {
    records: [
      {
        ...record,
        envelope: {
          ...record.envelope,
          statement: "Foreign evidence must remain hidden",
          context: {
            ...record.envelope.context,
            repository_id: "9".repeat(32),
          },
        },
      },
    ],
  });
  await expect(page.getByRole("alert")).toContainText(
    "Learning records could not be read",
  );
  await expect(
    page.getByText("Foreign evidence must remain hidden"),
  ).toHaveCount(0);
});
test("projection denial preserves local provenance without claiming a verified remote source", async ({
  page,
}) => {
  await open(page, { records: [record], projectionDenied: true });
  await page
    .getByRole("button", { name: "Verify inventory reservation", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Projection read permission denied",
  );
  await expect(page.locator("main")).toContainText(record.envelope.statement);
  await expect(
    page.getByText("Verified projection", { exact: true }),
  ).toHaveCount(0);
});
test("observed failure candidates do not dispatch a local-outcome projection read", async ({
  page,
}) => {
  const candidate = {
    ...record,
    state: "candidate",
    kind: "observed_failure",
    observed_at: "2026-10-14T12:00:00Z",
    verified_at: undefined,
    envelope: { ...record.envelope, type: "observed_failure" },
  };
  const calls = await open(page, { records: [candidate] });
  await page
    .getByRole("button", { name: "Verify inventory reservation", exact: true })
    .click();
  await expect(page.locator("main")).toContainText("It cannot authorize reuse");
  expect(calls.some((call) => call.endsWith("/projection"))).toBe(false);
});
test("short landscape opens the evidence list within the available viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 812, height: 375 });
  await open(page, { records: [record] });
  await expect(
    page.getByRole("button", { name: record.envelope.question, exact: true }),
  ).toBeInViewport({ ratio: 0.8 });
  const first = await page
    .getByRole("button", { name: record.envelope.question, exact: true })
    .boundingBox();
  const footer = await page.locator(".workspace-main > footer").boundingBox();
  expect(first!.y + first!.height).toBeLessThanOrEqual(footer!.y + 1);
});
test("repository changes remove prior evidence and read only the newly selected repository", async ({
  page,
}) => {
  const calls = await open(page, { records: [record], alternate: true });
  await page
    .getByRole("button", { name: record.envelope.question, exact: true })
    .click();
  await expect(page.locator("main")).toContainText(record.envelope.statement);
  await page
    .getByLabel("Knowledge project", { exact: true })
    .selectOption("9".repeat(32));
  await expect(
    page.getByText("No learning records returned for this repository.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText(record.envelope.statement, { exact: true }),
  ).toHaveCount(0);
  expect(calls).toContain(
    "GET /api/v1/repositories/" + "9".repeat(32) + "/learning",
  );
  expect(calls.filter((call) => !call.startsWith("GET "))).toEqual([]);
});
test("local filters hide excluded evidence without dispatching a knowledge search or write", async ({
  page,
}) => {
  const calls = await open(page, { records: [record] });
  await page.locator(".learning-records-filter-toggle > summary").click();
  await page
    .getByLabel("Saved state", { exact: true })
    .selectOption("candidate");
  await expect(
    page.getByText("No saved records match this filter.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: record.envelope.question, exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Saved state", { exact: true }).selectOption("all");
  await page
    .getByLabel("Filter saved records", { exact: true })
    .fill("unrelated procedure");
  await expect(
    page.getByText("No saved records match this filter.", { exact: true }),
  ).toBeVisible();
  expect(calls.some((call) => call.includes("/knowledge/search"))).toBe(false);
  expect(calls.filter((call) => !call.startsWith("GET "))).toEqual([]);
});
for (const viewport of [
  { name: "wide", width: 1440, height: 900 },
  { name: "short", width: 1280, height: 500 },
  { name: "landscape", width: 812, height: 375 },
  { name: "mobile", width: 390, height: 844 },
]) {
  for (const theme of ["light", "dark"] as const) {
    test(`record provenance wraps and remains keyboard reachable in ${viewport.name} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (value) => localStorage.setItem("forge.appearance", value),
        theme,
      );
      const long = Array.from({ length: 30 }, (_, index) => ({
        ...record,
        id: (index + 1).toString(16).padStart(32, "0"),
        envelope: {
          ...record.envelope,
          question:
            `Reservation check ${index + 1}: ` +
            "scoped inventory provenance and version consistency ".repeat(4),
        },
      }));
      await open(page, {
        records: [record, ...long],
        projection: {
          id: "1".repeat(32),
          record_id: record.id,
          state: "verified",
          workspace_id: "owned-learning-" + "inventory-".repeat(8),
          source_id: "owned-source",
          source_status: "ingested",
          content_sha256: "2".repeat(64),
          citations: [{ source_id: "owned-source", chunk_id: "owned-chunk" }],
        },
      });
      const choice = page.getByRole("button", {
        name: "Verify inventory reservation",
        exact: true,
      });
      await expect(choice).toBeVisible();
      const prefix = process.env.FORGE_RECORDS_CAPTURE_DIR;
      if (prefix)
        await page.screenshot({
          path: `${prefix}/fixture-${viewport.name}-${theme}-list.png`,
        });
      await choice.focus();
      await page.keyboard.press("Enter");
      const heading = page.getByRole("heading", {
        name: "Verify inventory reservation",
        exact: true,
      });
      await expect(heading).toBeFocused();
      await expect(
        page.getByText("Verified projection", { exact: true }),
      ).toBeVisible();
      await page
        .getByText("Identifiers and verification receipts", { exact: true })
        .click();
      await expect(
        page.getByText("Journal sequence 6", { exact: true }),
      ).toBeVisible();
      const layout = await page.evaluate(() => {
        const main = document.querySelector(".workspace-main > main")!,
          footer = document.querySelector(".workspace-main > footer")!;
        return {
          documentWidth: document.documentElement.scrollWidth,
          viewport: innerWidth,
          mainBottom: main.getBoundingClientRect().bottom,
          footerTop: footer.getBoundingClientRect().top,
          listScrollable:
            document.querySelector(".learning-records-list")!.scrollHeight >
            document.querySelector(".learning-records-list")!.clientHeight,
        };
      });
      expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
      expect(layout.mainBottom).toBeLessThanOrEqual(layout.footerTop + 1);
      expect(layout.listScrollable).toBe(true);
      if (prefix)
        await page.screenshot({
          path: `${prefix}/fixture-${viewport.name}-${theme}-provenance.png`,
        });
    });
  }
}
