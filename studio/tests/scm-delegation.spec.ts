/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

const projectId = "a".repeat(32),
  repositoryId = "b".repeat(32);
const project = {
  id: projectId,
  revision: 1,
  digest: "c".repeat(64),
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  definition: {
    name: "Inventory product",
    description: "Fictional delegation fixture",
    owners: ["Fixture owner"],
    topology: "monorepo",
    components: [
      {
        id: "api",
        name: "Inventory API",
        repository_id: repositoryId,
        path: ".",
        kind: "api",
        backend: "pyfly",
        frontend: "none",
        verification: [],
        depends_on: [],
        responsibility: "Inventory requests",
      },
    ],
  },
};
async function setup(page: Page, reader = false) {
  const state = { writes: 0, delegationReads: 0 };
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      reader &&
      path.endsWith("/auth/session") &&
      route.request().headers().cookie?.includes("forge_session")
    )
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: "f".repeat(32),
            display_name: "Project reader",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: false,
          memberships: [
            { project_id: projectId, role: "viewer", permissions: ["read"] },
          ],
          roles: { viewer: ["read"] },
        },
      });
    if (await mockIdentity(route)) return;
    if (path.includes("scm-actor-bindings")) {
      if (route.request().method() === "GET") state.delegationReads++;
      else state.writes++;
      return route.fulfill({
        status: 404,
        json: {
          detail: "This controlled baseline has no delegation endpoint.",
        },
      });
    }
    return route.fulfill({
      json: path.endsWith("/projects")
        ? [project]
        : path.endsWith("/repositories")
          ? [
              {
                id: repositoryId,
                name: "Inventory repository",
                url: "https://github.example.test/fixture/inventory",
              },
            ]
          : path.endsWith("/health")
            ? {
                status: "ok",
                service_role: "api",
                admission_ready: true,
                execution_ready: false,
                detail: "",
                execution: { local_enabled: false },
              }
            : path.endsWith("/profiles")
              ? {}
              : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("fictional-scm-bootstrap-token-at-least24");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await navigate(page, "Project access");
  return state;
}

test("project access exposes explicit delegation administration without hidden reads or writes", async ({
  page,
}) => {
  const state = await setup(page);
  const panel = page.getByRole("region", {
    name: "Source-control delegation",
    exact: true,
  });
  await expect(panel).toBeVisible();
  await expect(
    panel.getByRole("button", {
      name: "Read current delegations",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "Review a delegation", exact: true }),
  ).toBeVisible();
  expect(state.delegationReads).toBe(0);
  expect(state.writes).toBe(0);
});

test("project readers can discover delegation reads without receiving administration controls", async ({
  page,
}) => {
  const state = await setup(page, true);
  const panel = page.getByRole("region", {
    name: "Source-control delegation",
    exact: true,
  });
  await expect(panel).toBeVisible();
  await expect(
    panel.getByRole("button", {
      name: "Read current delegations",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "Review a delegation", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByText("A factory administrator must review delegation changes.", {
      exact: true,
    }),
  ).toBeVisible();
  expect(state.delegationReads).toBe(0);
  expect(state.writes).toBe(0);
});

test("review retains the exact input before one write and confirms only through exact GET", async ({
  page,
}) => {
  const { setupDelegation, saveDelegation, digest, projectCut } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  await saveDelegation(page);
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "No current delegation heads are recorded for this project repository.",
      { exact: true },
    ),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(1);
  const body = state.posts[0];
  expect(body.expected_project_binding_digest).toBe(projectCut);
  expect(state.exactReads).toEqual([body.request_id]);
  expect(state.markersAtPost).toEqual([
    [
      {
        version: 1,
        base: new URL("/api/v1", page.url()).href,
        actor: "f".repeat(32),
        project: "5".repeat(32),
        repository: "6".repeat(32),
        requestId: body.request_id,
        inputDigest: digest(body),
      },
    ],
  ]);
  expect(state.writes).toEqual([]);
  await expect(
    page.getByText("Exact receipt confirmed", { exact: true }),
  ).toBeVisible();
});

test("lost delivery and reload recover only the original identity without a second POST", async ({
  page,
}) => {
  const { setupDelegation, saveDelegation } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  state.loseResponse = true;
  state.exactStatus = 404;
  await saveDelegation(page);
  await expect(
    page.getByText("Original result unconfirmed", { exact: true }),
  ).toBeVisible();
  const id = state.posts[0].request_id;
  await page.reload();
  await navigate(page, "Project access");
  await page
    .getByRole("button", { name: "Check original delegation 1", exact: true })
    .click();
  await expect(
    page.getByText(
      "Delegation information is unavailable on this server. This is not an empty or revoked configuration.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review a delegation", exact: true }),
  ).toBeDisabled();
  state.exactStatus = 200;
  await page
    .getByRole("button", { name: "Check original delegation 1", exact: true })
    .click();
  await expect(
    page.getByText("Exact receipt confirmed", { exact: true }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(state.exactReads).toEqual([id, id]);
});

for (const status of [201, 409])
  test(`POST ${status} with missing exact receipt holds the identity and offers no reset`, async ({
    page,
  }) => {
    const { setupDelegation, saveDelegation } =
      await import("./scm-delegation-fixture");
    const state = await setupDelegation(page);
    state.publishReceipt = false;
    state.postStatus = status;
    await saveDelegation(page);
    await expect(
      page.getByText("Original result unconfirmed", { exact: true }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Check original delegation 1", exact: true })
      .click();
    await expect(
      page.getByRole("button", { name: "Review a delegation", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: /reset|retry|replace/i }),
    ).toHaveCount(0);
    expect(state.posts).toHaveLength(1);
    expect(new Set(state.exactReads)).toEqual(
      new Set([state.posts[0].request_id]),
    );
  });

test("changed retained input after confirmation holds a new review and preserves the exact receipt", async ({
  page,
}) => {
  const { setupDelegation, saveDelegation } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  await saveDelegation(page);
  await expect(
    page.getByText("Exact receipt confirmed", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const key = Object.keys(localStorage).find((k) =>
      k.startsWith("forge.scm-delegation.v1:"),
    )!;
    const marker = JSON.parse(localStorage.getItem(key)!);
    marker.inputDigest = "a".repeat(64);
    localStorage.setItem(key, JSON.stringify(marker));
  });
  await page
    .getByRole("button", { name: "Review a delegation", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Review SCM delegation", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Review a delegation", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
});

test("current permission refusal clears private receipt but keeps its original request", async ({
  page,
}) => {
  const { setupDelegation, saveDelegation } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  await saveDelegation(page);
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toBeVisible();
  state.exactStatus = 403;
  await page
    .getByRole("button", { name: "Check original delegation 1", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText(
      "Current access was refused. Private delegation details have been cleared.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((k) =>
          k.startsWith("forge.scm-delegation.v1:"),
        ).length,
    ),
  ).toBe(1);
  expect(state.posts).toHaveLength(1);
});

test("a different full stored project cut cannot open owner review", async ({
  page,
}) => {
  const { setupDelegation } = await import("./scm-delegation-fixture");
  const state = await setupDelegation(page, { cut: "a".repeat(64) });
  await page
    .getByRole("button", { name: "Review a delegation", exact: true })
    .click();
  await expect(
    page.getByText(
      "The delegation response could not be verified. Keep any original request for review.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("dialog", { name: "Review SCM delegation", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
});

test("a bounded person lookup cannot be relabeled while its response is pending", async ({
  page,
}) => {
  const { setupDelegation, nativeUser } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  await page
    .getByRole("button", { name: "Review a delegation", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Review SCM delegation",
    exact: true,
  });
  await dialog
    .getByLabel("Webhook-reported account", { exact: true })
    .selectOption("2".repeat(32));
  await dialog
    .getByRole("button", { name: "Continue to scope", exact: true })
    .click();
  await dialog
    .getByLabel("Find a registered person", { exact: true })
    .fill("Warehouse");
  let release!: () => void;
  state.eligibleGate = new Promise<void>((resolve) => (release = resolve));
  await dialog
    .getByRole("button", { name: "Find person", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Find a registered person", { exact: true }),
  ).toBeDisabled();
  release();
  await dialog
    .getByLabel("Registered person", { exact: true })
    .selectOption(nativeUser);
  await expect(
    dialog.getByLabel("Find a registered person", { exact: true }),
  ).toHaveValue("Warehouse");
});

test("storage refusal before dispatch sends nothing and never invents a replacement", async ({
  page,
}) => {
  const { setupDelegation, delegationReview } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  const dialog = await delegationReview(page);
  await dialog
    .getByRole("checkbox", {
      name: "I trust this webhook assertion.",
      exact: true,
    })
    .check();
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("forge.scm-delegation.v1:"))
        throw new DOMException("Fixture quota", "QuotaExceededError");
      original.call(this, key, value);
    };
  });
  await dialog
    .getByRole("button", { name: "Save delegation", exact: true })
    .click();
  await expect(
    dialog.getByText(
      "The request could not be retained safely. No new delegation request was sent.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toHaveLength(0);
});

test("historical current-head reading binds its original administrator rather than its current reader", async ({
  page,
}) => {
  const { setupDelegation, receiptFor, headFor, projectCut } =
    await import("./scm-delegation-fixture");
  const packet = (
    await import("./scm-delegation-contract-fixture.json", {
      with: { type: "json" },
    })
  ).default;
  const receipt = receiptFor(
    {
      ...packet.request,
      expected_project_binding_digest: projectCut,
    } as import("../src/ScmDelegationData").DelegationInput,
    "7".repeat(32),
  );
  const state = await setupDelegation(page, {
    reader: true,
    heads: [headFor(receipt, "stale_project")],
    initialReceipt: receipt,
  });
  await page
    .getByRole("button", { name: "Read current delegations", exact: true })
    .click();
  await expect(
    page.getByText("Project changed · revision 1", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Read exact revision 1", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review a delegation", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
  expect(state.exactReads).toEqual([receipt.request.request_id]);
});

test("late exact read after navigation cannot repaint private delegation details", async ({
  page,
}) => {
  const { setupDelegation, saveDelegation } =
    await import("./scm-delegation-fixture");
  const state = await setupDelegation(page);
  await saveDelegation(page);
  await expect(
    page.getByText("Exact receipt confirmed", { exact: true }),
  ).toBeVisible();
  let release!: () => void;
  state.exactGate = new Promise<void>((resolve) => (release = resolve));
  await page
    .getByRole("button", { name: "Check original delegation 1", exact: true })
    .click();
  await expect.poll(() => state.exactReads.length).toBe(2);
  await navigate(page, "Factory");
  release();
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toHaveCount(0);
  await navigate(page, "Project access");
  await expect(
    page.getByText("Original result unconfirmed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Exact delegation receipt", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(1);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 600 },
  { width: 600, height: 390 },
  { width: 320, height: 400 },
])
  for (const theme of ["light", "dark"])
    test(`delegation review painted controls ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      const { setupDelegation, delegationReview } =
        await import("./scm-delegation-fixture");
      await page.setViewportSize(viewport);
      const state = await setupDelegation(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const dialog = await delegationReview(page);
      await page.evaluate(() => document.fonts.ready);
      const save = dialog.getByRole("button", {
        name: "Save delegation",
        exact: true,
      });
      await save.focus();
      await expect
        .poll(() =>
          dialog.evaluate((node) => {
            const r = node.getBoundingClientRect(),
              body = node.querySelector(".wizard-field-scroll")!,
              actions = node.querySelector(".wizard-actions")!,
              a = actions.getBoundingClientRect();
            return {
              inside:
                r.top >= 0 &&
                r.bottom <= innerHeight + 1 &&
                r.left >= 0 &&
                r.right <= innerWidth + 1,
              overflow: body.scrollWidth <= body.clientWidth + 1,
              actions:
                a.top >= r.top &&
                a.bottom <= r.bottom + 1 &&
                [...actions.querySelectorAll("button")].every((button) => {
                  const b = button.getBoundingClientRect(),
                    hit = document.elementFromPoint(
                      (b.left + b.right) / 2,
                      (b.top + b.bottom) / 2,
                    );
                  return (
                    b.top >= r.top &&
                    b.bottom <= r.bottom + 1 &&
                    b.bottom <= innerHeight &&
                    b.left >= r.left &&
                    b.right <= r.right + 1 &&
                    (hit === button || button.contains(hit))
                  );
                }),
            };
          }),
        )
        .toEqual({ inside: true, overflow: true, actions: true });
      const ack = dialog.getByRole("checkbox", {
        name: "I trust this webhook assertion.",
        exact: true,
      });
      await ack.focus();
      await expect
        .poll(() =>
          ack.locator("..").evaluate((node) => {
            const r = node.getBoundingClientRect(),
              field = node
                .closest(".wizard-field-scroll")!
                .getBoundingClientRect();
            return r.top >= field.top - 1 && r.bottom <= field.bottom + 1;
          }),
        )
        .toBe(true);
      await page.keyboard.press("Space");
      await expect(ack).toBeChecked();
      await page.keyboard.press("Tab");
      await expect(
        dialog.getByRole("button", { name: "Cancel", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(save).toBeFocused();
      await expect
        .poll(() =>
          save.evaluate((node) => {
            const r = node.getBoundingClientRect(),
              form = node.closest(".wizard-form")!.getBoundingClientRect(),
              style = getComputedStyle(node),
              outset =
                parseFloat(style.outlineWidth) +
                parseFloat(style.outlineOffset);
            return (
              r.left - outset >= form.left &&
              r.right + outset <= form.right &&
              r.top - outset >= form.top &&
              r.bottom + outset <= form.bottom
            );
          }),
        )
        .toBe(true);
      await page.screenshot({
        path: info.outputPath(`review-${viewport.width}-${theme}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await expect(
        page.getByRole("button", { name: "Review a delegation", exact: true }),
      ).toBeFocused();
      expect(state.posts).toHaveLength(0);
    });

test("delegation review remains keyboard accessible at 200 percent zoom", async ({
  page,
}, info) => {
  const { setupDelegation, delegationReview } =
    await import("./scm-delegation-fixture");
  await page.setViewportSize({ width: 1280, height: 900 });
  await setupDelegation(page);
  await page.evaluate(() => (document.documentElement.style.zoom = "2"));
  const dialog = await delegationReview(page),
    ack = dialog.getByRole("checkbox", {
      name: "I trust this webhook assertion.",
      exact: true,
    });
  await ack.focus();
  await page.keyboard.press("Space");
  await expect(ack).toBeChecked();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Tab");
  const save = dialog.getByRole("button", {
    name: "Save delegation",
    exact: true,
  });
  await expect(save).toBeFocused();
  await expect
    .poll(() =>
      save.evaluate((node) => {
        const r = node.getBoundingClientRect(),
          hit = document.elementFromPoint(
            (r.left + r.right) / 2,
            (r.top + r.bottom) / 2,
          );
        return (
          r.top >= 0 &&
          r.bottom <= innerHeight &&
          r.left >= 0 &&
          r.right <= innerWidth &&
          (hit === node || node.contains(hit))
        );
      }),
    )
    .toBe(true);
  await page.screenshot({ path: info.outputPath("review-zoom200.png") });
});

test("an administrator reads the explicit prior head before saving a CAS-bound revision", async ({
  page,
}) => {
  const { setupDelegation, receiptFor, headFor, projectCut, saveDelegation } =
    await import("./scm-delegation-fixture");
  const packet = (
    await import("./scm-delegation-contract-fixture.json", {
      with: { type: "json" },
    })
  ).default;
  const original = receiptFor(
    {
      ...packet.request,
      active: false,
      expected_project_binding_digest: projectCut,
    } as import("../src/ScmDelegationData").DelegationInput,
    "7".repeat(32),
  );
  const state = await setupDelegation(page, {
    heads: [headFor(original, "revoked")],
    initialReceipt: original,
  });
  await saveDelegation(page);
  await expect(
    page.getByRole("heading", {
      name: "Recorded delegation revision 2",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(state.posts[0].expected_previous_digest).toBe(original.digest);
  expect(state.exactReads).toEqual([
    original.request.request_id,
    state.posts[0].request_id,
  ]);
  expect(state.posts[0].active).toBe(false);
});

for (const width of [1440, 390])
  for (const theme of ["light", "dark"])
    test(`delegation journey states ${width} ${theme}`, async ({
      page,
    }, info) => {
      const { setupDelegation, delegationReview } =
        await import("./scm-delegation-fixture");
      await page.setViewportSize({ width, height: width === 1440 ? 900 : 600 });
      const state = await setupDelegation(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const dialog = await delegationReview(page, async (stage) => {
        const modal = page.getByRole("dialog", {
          name: "Review SCM delegation",
          exact: true,
        });
        await expect
          .poll(() =>
            modal.evaluate((node) => {
              const r = node.getBoundingClientRect(),
                field = node.querySelector(".wizard-field-scroll")!,
                actions = node
                  .querySelector(".wizard-actions")!
                  .getBoundingClientRect();
              return (
                r.top >= 0 &&
                r.bottom <= innerHeight + 1 &&
                r.left >= 0 &&
                r.right <= innerWidth + 1 &&
                field.scrollWidth <= field.clientWidth + 1 &&
                actions.bottom <= r.bottom + 1
              );
            }),
          )
          .toBe(true);
        await page.screenshot({
          path: info.outputPath(`${stage}-${width}-${theme}.png`),
        });
      });
      await dialog
        .getByRole("checkbox", {
          name: "I trust this webhook assertion.",
          exact: true,
        })
        .check();
      await dialog
        .getByRole("button", { name: "Save delegation", exact: true })
        .click();
      const receipt = page.getByRole("region", {
        name: "Exact delegation receipt",
        exact: true,
      });
      await expect(receipt).toBeVisible();
      await receipt.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath(`receipt-${width}-${theme}.png`),
      });
      expect(state.posts).toHaveLength(1);
      expect(state.exactReads).toEqual([state.posts[0].request_id]);
    });
