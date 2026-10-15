/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { writeFileSync } from "node:fs";
import { record } from "./learning-record-fixture";
const actor = "9".repeat(32),
  source = "owned-procedure-source";
const projection = {
  id: "1".repeat(32),
  record_id: record.id,
  state: "source_admitted",
  request_id: "2".repeat(32),
  workspace_id: "forge-learning-owned-inventory",
  source_id: source,
  source_status: "queued",
  payload_digest: "3".repeat(64),
  content_sha256: "4".repeat(64),
  target: { revision: "5".repeat(32) },
  citations: [],
};
async function fixture(
  page: Page,
  options: {
    lost?: boolean;
    receipt?: string;
    viewer?: boolean;
    storage?: boolean;
    projectionState?: string;
    holdReceipt?: boolean;
    denyProjectionAfterReceipt?: boolean;
    currentRecordState?: "verified" | "invalidated";
  } = {},
) {
  let releaseReceipt = () => {};
  const heldReceipt = new Promise<void>((done) => {
    releaseReceipt = done;
  });
  let denialStatus = 403;
  let writes = 0,
    reads = 0,
    denied = false,
    currentActor = actor,
    body: { request_id: string; source_id: string } | null = null;
  if (options.storage)
    await page.addInitScript(() => {
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith("forge.learning-observation"))
          throw new Error("Storage unavailable");
        return set.call(this, key, value);
      };
    });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({ json: { mode: "local_bootstrap" } });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: currentActor,
            display_name: "Owned reviewer",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: false,
          memberships: [
            {
              project_id: record.envelope.context.project_id,
              role: options.viewer ? "viewer" : "reviewer",
              revision: 1,
              permissions: options.viewer ? ["read"] : ["read", "review"],
            },
          ],
          roles: {},
        },
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: record.envelope.context.repository_id,
            name: "Owned inventory",
            url: "/owned/inventory",
            provider: "local",
            default_branch: "main",
          },
        ],
      });
    if (path.endsWith("/learning") && path.includes("/repositories/"))
      return route.fulfill({
        json: [
          {
            ...record,
            state: writes
              ? (options.currentRecordState ?? "verified")
              : "verified",
          },
        ],
      });
    if (
      path.endsWith("/projection") &&
      options.denyProjectionAfterReceipt &&
      reads
    )
      return route.fulfill({
        status: 403,
        json: { detail: "Projection read denied" },
      });
    if (path.endsWith("/projection"))
      return route.fulfill({
        json: {
          ...projection,
          state: writes
            ? "verified"
            : (options.projectionState ?? "source_admitted"),
          citations:
            writes || options.projectionState === "verified"
              ? [{ source_id: source, chunk_id: "owned-chunk:1" }]
              : [],
        },
      });
    if (path.endsWith("/projection/observe")) {
      writes++;
      body = req.postDataJSON();
      if (options.lost) return route.abort("failed");
      return route.fulfill({ json: { ...projection, state: "verified" } });
    }
    if (path.includes("/projection/observations/")) {
      reads++;
      if (options.holdReceipt) await heldReceipt;
      if (denied)
        return route.fulfill({
          status: denialStatus,
          json: { detail: "Review authority revoked" },
        });
      if (options.receipt === "absent")
        return route.fulfill({
          status: 404,
          json: { detail: "Exact observation request not observed" },
        });
      const receipt = {
        event_id: "6".repeat(32),
        record_id: record.id,
        repository_id: record.envelope.context.repository_id,
        run_id: record.envelope.run_id,
        request_id: body!.request_id,
        kind: "projection_observation",
        event_state: "verified",
        current_record_state: options.currentRecordState ?? "verified",
        current_projection_state: "verified",
        actor_id: actor,
        projection_id: projection.id,
        submission_request_id: projection.request_id,
        workspace_id: projection.workspace_id,
        source_id: body!.source_id,
        target_revision: projection.target.revision,
        payload_digest: projection.payload_digest,
        content_sha256: projection.content_sha256,
        citations: [{ source_id: body!.source_id, chunk_id: "owned-chunk:1" }],
        created_at: "2026-10-15T06:00:00Z",
        sequence: 4,
        digest: "7".repeat(64),
      };
      if (options.receipt === "request") receipt.request_id = "8".repeat(32);
      if (options.receipt === "source") receipt.source_id = "different-source";
      if (options.receipt === "actor") receipt.actor_id = "8".repeat(32);
      if (options.receipt === "target")
        receipt.target_revision = "8".repeat(32);
      return route.fulfill({
        json:
          options.receipt === "legacy"
            ? { ...receipt, actor_id: null }
            : receipt,
      });
    }
    return route.fulfill({
      json: [
        "/runs",
        "/projects",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((s) => path.endsWith(s))
        ? []
        : {},
    });
  });
  return {
    writes: () => writes,
    reads: () => reads,
    body: () => body,
    deny: (status = 403) => {
      denied = true;
      denialStatus = status;
    },
    releaseReceipt: () => releaseReceipt(),
    changeActor: () => {
      currentActor = "8".repeat(32);
    },
  };
}
async function select(page: Page, reload = true) {
  if (reload) await page.goto("/");
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Learning records", exact: true })
    .click();
  await page
    .getByRole("button", { name: record.envelope.question, exact: true })
    .click();
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Verify existing source", exact: true })
    .click();
  await page
    .getByLabel("Existing source identifier", { exact: true })
    .fill(source);
  await page
    .getByRole("button", { name: "Review source", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Verify source",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I understand this reads the existing source and records a local verification; it does not submit or ingest a source.",
      { exact: true },
    )
    .check();
}
test("one observation requires its exact receipt even after a successful POST", async ({
  page,
}) => {
  const state = await fixture(page);
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(1);
  expect(state.body()).toEqual({
    request_id: expect.stringMatching(/^[a-f0-9]{32}$/),
    source_id: source,
  });
  await expect(
    page.getByText(`Source: ${source}`, { exact: true }).first(),
  ).toBeVisible();
});
for (const receipt of [
  "absent",
  "request",
  "source",
  "actor",
  "target",
  "legacy",
])
  test(`lost response with ${receipt} receipt never permits replay after reload`, async ({
    page,
  }) => {
    const state = await fixture(page, { lost: true, receipt });
    await select(page);
    await review(page);
    await page
      .getByRole("button", { name: "Verify source", exact: true })
      .click();
    await expect(
      page.getByText("Original source verification unresolved", {
        exact: true,
      }),
    ).toBeVisible();
    const original = state.body();
    await select(page);
    await expect(
      page.getByText("Original source verification unresolved", {
        exact: true,
      }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Check original verification", exact: true })
      .click();
    await expect(page.getByRole("alert")).toBeVisible();
    expect(state.writes()).toBe(1);
    expect(state.body()).toEqual(original);
    await expect(
      page.getByRole("button", { name: "Verify existing source", exact: true }),
    ).toHaveCount(0);
  });
test("storage refusal prevents the observation POST", async ({ page }) => {
  const state = await fixture(page, { storage: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "No observation was submitted",
  );
  expect(state.writes()).toBe(0);
});
test("a denied exact receipt clears private evidence and retains the marker", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification unresolved", { exact: true }),
  ).toBeVisible();
  state.deny();
  await page
    .getByRole("button", { name: "Check original verification", exact: true })
    .click();
  await expect(
    page.getByText(/Private evidence has been cleared/),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: record.envelope.question, exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        Object.keys(sessionStorage).filter((k) =>
          k.startsWith("forge.learning-observation"),
        ).length,
    ),
  ).toBe(1);
  expect(state.writes()).toBe(1);
});
test("read-only membership cannot open source verification", async ({
  page,
}) => {
  await fixture(page, { viewer: true });
  await select(page);
  await expect(
    page.getByRole("button", { name: "Verify existing source", exact: true }),
  ).toHaveCount(0);
});
for (const state of ["pending", "executing", "verified", "abandoned"])
  test(`saved ${state} state does not offer a new observation`, async ({
    page,
  }) => {
    await fixture(page, { projectionState: state });
    await select(page);
    await expect(
      page.getByRole("button", { name: "Verify existing source", exact: true }),
    ).toHaveCount(0);
  });
test("a lost response can be confirmed after reload by one exact read and no replay", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification unresolved", { exact: true }),
  ).toBeVisible();
  const original = state.body();
  await select(page);
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Check original verification", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toBeVisible();
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(1);
  expect(state.body()).toEqual(original);
});
test("a changed stored identity cannot dispatch a receipt read or another observation", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification unresolved", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    const key = Object.keys(sessionStorage).find((k) =>
      k.startsWith("forge.learning-observation"),
    )!;
    const marker = JSON.parse(sessionStorage.getItem(key)!);
    marker.requestId = "8".repeat(32);
    sessionStorage.setItem(key, JSON.stringify(marker));
  });
  await page
    .getByRole("button", { name: "Check original verification", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(state.reads()).toBe(0);
  expect(state.writes()).toBe(1);
});
test("an actor change while an exact read is pending cannot reveal its old confirmation", async ({
  page,
}) => {
  const state = await fixture(page, { lost: true, holdReceipt: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification unresolved", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Check original verification", exact: true })
    .click();
  await expect.poll(() => state.reads()).toBe(1);
  state.changeActor();
  await select(page);
  state.releaseReceipt();
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", {
      name: "Check original verification",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.writes()).toBe(1);
});
test("a confirmed historical observation remains selected when current evidence no longer matches the filter", async ({
  page,
}) => {
  const state = await fixture(page, { currentRecordState: "invalidated" });
  await select(page);
  await page.locator(".learning-records-filter-toggle > summary").click();
  await page
    .getByLabel("Saved state", { exact: true })
    .selectOption("verified");
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      /Historical observation: verified. Current record state: invalidated/,
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: record.envelope.question, exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Close selected record", exact: true })
    .click();
  await expect(
    page.locator(".learning-records-filter-toggle > summary"),
  ).toBeFocused();
  expect(state.writes()).toBe(1);
});
async function visibleControl(page: Page, selector: string) {
  const control = page.locator(selector);
  await control.scrollIntoViewIfNeeded();
  const bounds = await control.evaluate((element) => {
    const box = element.getBoundingClientRect();
    let top = 0,
      bottom = innerHeight,
      left = 0,
      right = innerWidth;
    const ancestors: unknown[] = [];
    for (let node = element.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node),
        rect = node.getBoundingClientRect();
      ancestors.push({
        className: node.className,
        top: rect.top,
        bottom: rect.bottom,
        height: rect.height,
        clientTop: node.clientTop,
        clientHeight: node.clientHeight,
        scrollHeight: node.scrollHeight,
        scrollTop: node.scrollTop,
        overflowY: style.overflowY,
        borderTop: style.borderTopWidth,
        borderBottom: style.borderBottomWidth,
        paddingTop: style.paddingTop,
        paddingBottom: style.paddingBottom,
        flexShrink: style.flexShrink,
      });
      if (/hidden|auto|scroll|clip/.test(style.overflowY)) {
        top = Math.max(top, rect.top + parseFloat(style.borderTopWidth));
        bottom = Math.min(
          bottom,
          rect.bottom - parseFloat(style.borderBottomWidth),
        );
      }
      if (/hidden|auto|scroll|clip/.test(style.overflowX)) {
        left = Math.max(left, rect.left + parseFloat(style.borderLeftWidth));
        right = Math.min(
          right,
          rect.right - parseFloat(style.borderRightWidth),
        );
      }
      if (node instanceof HTMLDialogElement && node.open) break;
    }
    return {
      top: box.top,
      bottom: box.bottom,
      left: box.left,
      right: box.right,
      ancestors,
      visibleTop: top,
      visibleBottom: bottom,
      visibleLeft: left,
      visibleRight: right,
      // Rounded corners are outside the painted control; test its straight edges.
      borderRadius: getComputedStyle(element).borderRadius,
      hitTop: element.contains(
        document.elementFromPoint(box.left + box.width / 2, box.top + 1),
      ),
      hitBottom: element.contains(
        document.elementFromPoint(box.left + box.width / 2, box.bottom - 1),
      ),
      hit: element.contains(
        document.elementFromPoint(
          box.left + box.width / 2,
          box.top + box.height / 2,
        ),
      ),
    };
  });
  await test.info().attach("control-geometry", {
    body: JSON.stringify(bounds, null, 2),
    contentType: "application/json",
  });
  writeFileSync(
    test.info().outputPath(`control-${selector.replace(/\W/g, "_")}.json`),
    JSON.stringify(bounds, null, 2),
  );
  if (bounds.bottom > bounds.visibleBottom)
    await page.screenshot({
      path: test.info().outputPath("clipped-control.png"),
    });
  expect(bounds.top).toBeGreaterThanOrEqual(bounds.visibleTop);
  expect(bounds.bottom).toBeLessThanOrEqual(bounds.visibleBottom);
  expect(bounds.left).toBeGreaterThanOrEqual(bounds.visibleLeft);
  expect(bounds.right).toBeLessThanOrEqual(bounds.visibleRight);
  expect(bounds.hit).toBe(true);
  expect(bounds.hitTop).toBe(true);
  expect(bounds.hitBottom).toBe(true);
  return bounds;
}
for (const viewport of [
  { name: "desktop", width: 1440, height: 900 },
  { name: "phone", width: 390, height: 844 },
  { name: "landscape", width: 844, height: 390 },
])
  for (const theme of ["light", "dark"] as const)
    test(`existing-source verification stays readable and keyboard reachable on ${viewport.name} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await page.addInitScript(
        (theme) => localStorage.setItem("forge.appearance", theme),
        theme,
      );
      const state = await fixture(page, { lost: true });
      await select(page);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      await page
        .getByRole("button", { name: "Verify existing source", exact: true })
        .click();
      const input = page.getByLabel("Existing source identifier", {
        exact: true,
      });
      await expect(input).toHaveValue(source);
      await visibleControl(page, "#learning-observation-source");
      await page.screenshot({
        path: testInfo.outputPath(
          `fixture-${viewport.name}-${theme}-source.png`,
        ),
      });
      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", {
          name: "Verify existing source",
          exact: true,
        }),
      ).toBeFocused();
      await review(page);
      await visibleControl(
        page,
        ".learning-observation-dialog .wizard-actions .primary",
      );
      await visibleControl(
        page,
        ".learning-observation-dialog .learning-invalidation-ack input",
      );
      await page.screenshot({
        path: testInfo.outputPath(
          `fixture-${viewport.name}-${theme}-review.png`,
        ),
      });
      await page
        .getByRole("button", {
          name: "Verify source",
          exact: true,
        })
        .click();
      const unresolved = page.getByText(
        "Original source verification unresolved",
        { exact: true },
      );
      await expect(unresolved).toBeVisible();
      await page.locator(".learning-record-observation").evaluate((section) => {
        const main = section.closest("main")!;
        main.scrollTop +=
          section.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      await expect(unresolved).toBeInViewport();
      await page.screenshot({
        path: testInfo.outputPath(
          `fixture-${viewport.name}-${theme}-unresolved.png`,
        ),
      });
      await page
        .getByRole("button", {
          name: "Check original verification",
          exact: true,
        })
        .click();
      const confirmed = page.getByText(
        "Original source verification confirmed",
        { exact: true },
      );
      await expect(confirmed).toBeVisible();
      await page.locator(".learning-record-observation").evaluate((section) => {
        const main = section.closest("main")!;
        main.scrollTop +=
          section.getBoundingClientRect().top -
          main.getBoundingClientRect().top -
          16;
      });
      await expect(confirmed).toBeInViewport();
      await page.screenshot({
        path: testInfo.outputPath(
          `fixture-${viewport.name}-${theme}-confirmed.png`,
        ),
      });
      expect(state.writes()).toBe(1);
      expect(state.reads()).toBe(1);
      await expect(page.locator("main")).toHaveJSProperty(
        "scrollWidth",
        await page.locator("main").evaluate((element) => element.clientWidth),
      );
    });

test("a denied post-confirmation status refresh clears the private confirmation and evidence", async ({
  page,
}) => {
  const state = await fixture(page, { denyProjectionAfterReceipt: true });
  await select(page);
  await review(page);
  await page
    .getByRole("button", { name: "Verify source", exact: true })
    .click();
  await expect(
    page.getByText(/Private evidence has been cleared/),
  ).toBeVisible();
  await expect(
    page.getByText("Original source verification confirmed", { exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: record.envelope.question, exact: true }),
  ).toHaveCount(0);
  expect(state.writes()).toBe(1);
  expect(state.reads()).toBe(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(sessionStorage).filter((k) =>
          k.startsWith("forge.learning-observation"),
        ).length,
    ),
  ).toBe(1);
});
