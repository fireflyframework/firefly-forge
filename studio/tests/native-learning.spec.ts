/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mockFactoryDashboard } from "./factory-dashboard-fixture";
import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdirSync } from "node:fs";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";
import {
  nativeRecord,
  nativeProjection,
  nativeVerifiedProjection,
  nativeReceipt,
  nativeFailureRecord,
  nativeFailureProjection,
  nativeFailureReceipt,
  nativeFailureVersion,
} from "./native-learning-fixture";

async function open(
  page: Page,
  options: { lost?: boolean; denied?: boolean } = {},
) {
  const state = {
    posts: 0,
    requests: new Map<string, string>(),
    calls: [] as string[],
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route)) return;
    const request = route.request(),
      path = new URL(request.url()).pathname;
    state.calls.push(request.method() + " " + path);
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({ json: { mode: "local_bootstrap" } });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Fixture reviewer",
            email: null,
            authentication_method: "api_token",
          },
          global_admin: false,
          memberships: [
            {
              project_id: nativeRecord.envelope.context.project_id,
              role: "maintainer",
              revision: 1,
              permissions: ["read", "edit", "review"],
            },
          ],
          roles: {},
        },
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: nativeRecord.envelope.context.repository_id,
            name: "Fixture inventory",
            url: "/fixture/inventory",
            provider: "local",
            default_branch: "main",
          },
        ],
      });
    if (path.endsWith("/knowledge/configuration"))
      return route.fulfill({
        status: 403,
        json: { detail: "Factory administration required" },
      });
    if (
      path.endsWith(
        "/" + nativeRecord.envelope.context.repository_id + "/learning",
      )
    )
      return route.fulfill({ json: [nativeRecord, nativeFailureRecord] });
    const recordId = path.split("/")[5];
    const failure = recordId === nativeFailureRecord.id;
    if (path.endsWith("/projection/observe")) {
      expect(request.method()).toBe("POST");
      const body = request.postDataJSON();
      const saved = await page.evaluate(() =>
        Object.keys(sessionStorage)
          .filter((key) =>
            key.startsWith("forge.learning-native-observation.v2:"),
          )
          .map((key) => JSON.parse(sessionStorage.getItem(key)!)),
      );
      const marker = saved.find((item) => item.record === recordId);
      expect(marker).toBeTruthy();
      expect(marker.requestId).toBe(body.request_id);
      expect(marker.bodyDigest).toBe(
        createHash("sha256")
          .update(
            JSON.stringify({
              request_id: body.request_id,
              source_id: body.source_id,
            }),
          )
          .digest("hex"),
      );
      expect(body.source_id).toBe(
        failure
          ? nativeFailureProjection.source_id
          : nativeProjection.source_id,
      );
      state.posts++;
      state.requests.set(recordId, body.request_id);
      if (options.lost) return route.abort("connectionfailed");
      if (options.denied)
        return route.fulfill({
          status: 403,
          json: { detail: "Current scope was revoked" },
        });
      return route.fulfill({
        json: {
          ...(failure ? nativeFailureProjection : nativeVerifiedProjection),
          state: "verified",
        },
      });
    }
    if (path.includes("/projection/observations/")) {
      expect(request.method()).toBe("GET");
      const id = path.split("/").at(-1)!;
      expect(id).toBe(state.requests.get(recordId));
      return route.fulfill({
        json: failure ? nativeFailureReceipt(id) : nativeReceipt(id),
      });
    }
    if (path.endsWith("/projection")) {
      const id = state.requests.get(recordId);
      return route.fulfill({
        json: failure
          ? id
            ? {
                ...nativeFailureProjection,
                state: "verified",
                reconciliation_id: id,
                source_versions: [nativeFailureVersion],
                version_state: "recorded",
                citations: nativeFailureReceipt(id).citations,
              }
            : nativeFailureProjection
          : id
            ? { ...nativeVerifiedProjection, reconciliation_id: id }
            : nativeProjection,
      });
    }
    expect(request.method()).toBe("GET");
    return route.fulfill({
      json: [
        "/runs",
        "/projects",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
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
  await page
    .getByRole("button", { name: nativeRecord.envelope.question, exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Koinon source evidence" }),
  ).toBeVisible();
  return state;
}
async function capture(page: Page, name: string) {
  const match =
    /^(scope|review|recorded|failure)-(desktop|mobile)-(light|dark)$/.exec(
      name,
    );
  if (!match) throw new Error("Unknown native learning capture stage");
  const selectors = {
    scope: ".native-learning-saved-scope summary",
    review: ".native-learning-review .primary",
    recorded: ".native-learning-result button",
    failure: ".native-learning-result button",
  };
  const stage = match[1] as keyof typeof selectors;
  await captureWalkthrough(
    page,
    `native-learning-${stage}`,
    match[3],
    match[2],
    {
      producer: "studio/tests/native-learning.spec.ts",
      actionSelector: selectors[stage],
      ...(stage === "scope"
        ? {}
        : {
            focusSelector:
              stage === "review"
                ? ".native-learning-review"
                : ".native-learning-result",
          }),
    },
  );
  const directory = process.env.FORGE_NATIVE_LEARNING_CAPTURE_DIR;
  if (!directory) return;
  mkdirSync(directory, { recursive: true });
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${directory}/${name}.png` });
}
async function fits(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  const footer = await page.locator(".workspace-main > footer").boundingBox();
  for (const button of await page
    .locator(".native-learning button:visible")
    .all()) {
    await button.scrollIntoViewIfNeeded();
    const rect = await button.boundingBox();
    expect(rect).not.toBeNull();
    expect(rect!.y + rect!.height).toBeLessThanOrEqual(footer!.y + 1);
  }
}
for (const device of ["desktop", "mobile"] as const)
  for (const theme of ["light", "dark"] as const) {
    test(`native learning observer fits ${device} ${theme}`, async ({
      page,
    }) => {
      await page.setViewportSize({
        width: device === "desktop" ? 1440 : 390,
        height: 900,
      });
      await page.addInitScript(
        (value) => localStorage.setItem("forge.appearance", value),
        theme,
      );
      const errors: string[] = [];
      page.on("pageerror", (error) => errors.push(error.message));
      const state = await open(page);
      await page.getByText("Saved scope and source", { exact: true }).click();
      await page.locator(".native-learning").scrollIntoViewIfNeeded();
      await capture(page, `scope-${device}-${theme}`);
      await page.getByText("Saved scope and source", { exact: true }).click();
      const review = page.getByRole("button", {
        name: "Review source observation",
      });
      await review.focus();
      await page.keyboard.press("Enter");
      const acknowledgement = page.getByRole("checkbox", {
        name: /I checked this existing source/,
      });
      await acknowledgement.focus();
      await page.keyboard.press("Space");
      const observe = page.getByRole("button", {
        name: "Observe this source",
        exact: true,
      });
      await observe.scrollIntoViewIfNeeded();
      await capture(page, `review-${device}-${theme}`);
      await observe.focus();
      await page.keyboard.press("Enter");
      await expect(
        page.getByRole("heading", {
          name: "Source observation recorded",
          exact: true,
        }),
      ).toBeVisible();
      expect(state.posts).toBe(1);
      await page.locator(".native-learning-result").scrollIntoViewIfNeeded();
      await expect(
        page.getByText("inventory-version-7", { exact: true }),
      ).toBeVisible();
      await capture(page, `recorded-${device}-${theme}`);
      await fits(page);
      await page
        .getByRole("button", {
          name: nativeFailureRecord.envelope.question,
          exact: true,
        })
        .click();
      await expect(page.getByText(/Its cause is not classified/)).toBeVisible();
      await page
        .getByRole("button", { name: "Review source observation" })
        .click();
      await page
        .getByRole("checkbox", { name: /I checked this existing source/ })
        .check();
      await page
        .getByRole("button", { name: "Observe this source", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Source observation recorded",
          exact: true,
        }),
      ).toBeVisible();
      await expect(page.locator(".native-learning-result strong")).toHaveText(
        "candidate",
      );
      await page.locator(".native-learning-result").scrollIntoViewIfNeeded();
      await capture(page, `failure-${device}-${theme}`);
      await fits(page);
      expect(state.posts).toBe(2);
      expect(errors).toEqual([]);
      expect(
        state.calls.some((call) => call.includes("/observation-attempts")),
      ).toBe(false);
    });
  }
test("a lost native observation reply survives reload and reads only its original receipt", async ({
  page,
}) => {
  const state = await open(page, { lost: true });
  await page.getByRole("button", { name: "Review source observation" }).click();
  await page
    .getByRole("checkbox", { name: /I checked this existing source/ })
    .check();
  await page
    .getByRole("button", { name: "Observe this source", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Observation outcome unknown" }),
  ).toBeVisible();
  await page.reload();
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Learning records", exact: true })
    .click();
  await page
    .getByRole("button", { name: nativeRecord.envelope.question, exact: true })
    .click();
  await page.getByRole("button", { name: "Read exact observation" }).click();
  await expect(
    page.getByRole("heading", { name: "Source observation recorded" }),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});

test("current access refusal clears private native evidence without a replacement write", async ({
  page,
}) => {
  const state = await open(page, { denied: true });
  await page.getByRole("button", { name: "Review source observation" }).click();
  await page
    .getByRole("checkbox", { name: /I checked this existing source/ })
    .check();
  await page
    .getByRole("button", { name: "Observe this source", exact: true })
    .click();
  await expect(
    page.getByText(/Private evidence has been cleared/),
  ).toBeVisible();
  await expect(page.locator(".native-learning")).toHaveCount(0);
  expect(state.posts).toBe(1);
});

test("browser storage refusal removes the submission action before any POST", async ({
  page,
}) => {
  const state = await open(page);
  await page.getByRole("button", { name: "Review source observation" }).click();
  await page
    .getByRole("checkbox", { name: /I checked this existing source/ })
    .check();
  await page.evaluate(() =>
    Object.defineProperty(Storage.prototype, "setItem", {
      configurable: true,
      value() {
        throw new Error("Controlled storage refusal");
      },
    }),
  );
  await page
    .getByRole("button", { name: "Observe this source", exact: true })
    .click();
  await expect(page.getByText(/No observation was submitted/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Observe this source", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toBe(0);
});
