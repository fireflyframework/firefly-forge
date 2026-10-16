/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import v1 from "./scm-delegation-contract-fixture.json" with { type: "json" };
import packet from "./scm-product-change-contract-fixture.json" with { type: "json" };
import {
  setupDelegation,
  receiptFor,
  headFor,
  projectCut,
  digest,
} from "./scm-delegation-fixture";
import { navigate } from "./navigation";
import type { DelegationInput } from "../src/ScmDelegationData";
import type {
  ProductChangeInput,
  ProductChangeReceipt,
} from "../src/ScmProductChangeConsentData";

// Constructed, Python-model-shaped responses; these browser cases are not native/provider acceptance.
async function setup(page: Page, reader = false, existingSession = false) {
  const binding = receiptFor({
    ...v1.request,
    expected_project_binding_digest: projectCut,
  } as DelegationInput);
  const v1state = await setupDelegation(page, {
    reader,
    existingSession,
    heads: [headFor(binding)],
    initialReceipt: binding,
  });
  const state = {
    posts: [] as ProductChangeInput[],
    receipts: new Map<string, ProductChangeReceipt>(),
    current: null as ProductChangeReceipt | null,
    exactStatus: 200,
    postStatus: 201,
    loseResponse: false,
    markersAtPost: [] as unknown[][],
  };
  await page.route("**/scm-product-change-grants**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (req.method() === "POST") {
      const body = req.postDataJSON() as ProductChangeInput;
      state.posts.push(body);
      state.markersAtPost.push(
        await page.evaluate(() =>
          Object.entries(localStorage)
            .filter(([key]) =>
              key.startsWith("forge.scm-product-change-consent.v1:"),
            )
            .map(([, raw]) => JSON.parse(raw)),
        ),
      );
      const unsigned = {
        ...packet.receipt,
        event_id: "a".repeat(32),
        project_id: binding.project_id,
        repository_id: binding.repository_id,
        binding_key: binding.binding_key,
        native_user_id: binding.request.native_user_id,
        administering_user_id: v1state.actor,
        request: body,
        request_digest: digest(body),
        previous_digest: body.expected_previous_digest,
        revision: body.expected_previous_digest ? 2 : 1,
      };
      const { digest: ignored, ...rest } = unsigned;
      void ignored;
      const receipt = { ...rest, digest: digest(rest) } as ProductChangeReceipt;
      if (state.postStatus === 201) {
        state.receipts.set(body.request_id, receipt);
        state.current = receipt;
      }
      if (state.loseResponse) return route.abort("failed");
      return route.fulfill({
        status: state.postStatus,
        json:
          state.postStatus === 201
            ? receipt
            : { detail: "The reviewed cut changed." },
      });
    }
    const value = path.includes("/requests/")
      ? state.receipts.get(path.split("/").at(-1)!)
      : state.current;
    const status =
      path.includes("/requests/") && state.exactStatus !== 200
        ? state.exactStatus
        : value
          ? 200
          : 404;
    return route.fulfill({
      status,
      json: status === 200 ? value : { detail: "No exact receipt returned." },
    });
  });
  await page
    .getByRole("button", { name: "Read current delegations", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Read exact revision 1", exact: true })
    .click();
  const panel = page.getByRole("region", {
    name: "Whole-product edit consent",
    exact: true,
  });
  await expect(
    page
      .getByRole("region", { name: "Exact delegation receipt", exact: true })
      .getByRole("region", { name: "Whole-product edit consent", exact: true }),
  ).toHaveCount(0);
  await expect(panel).toBeVisible();
  return { state, panel, v1state, binding };
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Review product-edit consent", exact: true })
    .click();
  const modal = page.getByRole("dialog", {
    name: "Review whole-product edit consent",
    exact: true,
  });
  await expect(modal).toBeVisible();
  await modal
    .getByRole("button", { name: "Continue", exact: true })
    .scrollIntoViewIfNeeded();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    modal.getByRole("group", { name: "Whole-product component scope" }),
  ).toContainText("Inventory API");
  await expect(
    modal
      .getByRole("group", { name: "Whole-product component scope" })
      .getByRole("checkbox"),
  ).toHaveCount(0);
  await modal
    .getByRole("button", { name: "Continue", exact: true })
    .scrollIntoViewIfNeeded();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await modal.getByRole("checkbox", { name: /I reviewed this person/ }).check();
  return modal;
}
test("separate consent records one retained POST and confirms only through its exact receipt", async ({
  page,
}) => {
  const { state, panel, v1state } = await setup(page);
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(v1state.posts).toHaveLength(0);
  expect(state.markersAtPost[0]).toHaveLength(1);
  expect(state.posts[0].allowed_actions).toEqual(["product_change"]);
  expect(state.markersAtPost[0][0]).not.toHaveProperty("component_ids");
  await expect(
    panel.getByText(
      "Exact consent receipt confirmed. Saving consent does not change a product or start a run.",
      { exact: true },
    ),
  ).toBeVisible();
});
test("lost delivery survives reload and exact recovery without a second POST", async ({
  page,
}) => {
  const { state } = await setup(page);
  state.loseResponse = true;
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("unconfirmed");
  await page.reload();
  await navigate(page, "Project access");
  await page
    .getByRole("button", { name: "Read current delegations", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Read exact revision 1", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(
    page.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
});
test("conflict and exact 404 retain the unknown request and hold new consent", async ({
  page,
}) => {
  const { state, panel } = await setup(page);
  state.postStatus = 409;
  state.exactStatus = 404;
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(
    panel.getByRole("button", {
      name: "Review product-edit consent",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((key) =>
          key.startsWith("forge.scm-product-change-consent.v1:"),
        ).length,
    ),
  ).toBe(1);
});
test("a changed V1 head before final admission sends no consent", async ({
  page,
}) => {
  const { state, v1state, binding } = await setup(page);
  const modal = await review(page);
  v1state.heads = [headFor(binding, "stale_connection")];
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(modal).not.toBeVisible();
  await expect(page.getByRole("alert")).toContainText(
    "no longer the current active mapping",
  );
  expect(state.posts).toHaveLength(0);
});
test("project readers get current reads without consent authoring", async ({
  page,
}) => {
  const { panel, state } = await setup(page, true);
  await expect(
    panel.getByRole("button", {
      name: "Read current product-edit consent",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", {
      name: "Review product-edit consent",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
});
test("missing native coordination holds before the first POST", async ({
  page,
}) => {
  const { state } = await setup(page);
  await page.evaluate(() =>
    Object.defineProperty(navigator, "locks", {
      value: undefined,
      configurable: true,
    }),
  );
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "cannot coordinate consent",
  );
  expect(state.posts).toHaveLength(0);
});
async function expectReadableStep(page: Page) {
  const modal = page.getByRole("dialog", {
    name: "Review whole-product edit consent",
    exact: true,
  });
  const fields = modal.locator(".wizard-field-scroll");
  await fields.evaluate((node) => node.scrollIntoView({ block: "start" }));
  const geometry = await fields.evaluate((node) => {
    const zoom =
      parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
    const box = node.getBoundingClientRect();
    let top = Math.max(0, box.top),
      bottom = Math.min(window.innerHeight, box.bottom);
    let ancestor = node.parentElement;
    while (ancestor) {
      const style = getComputedStyle(ancestor);
      if (/hidden|auto|scroll|clip/.test(style.overflowY)) {
        const clip = ancestor.getBoundingClientRect();
        top = Math.max(top, clip.top + ancestor.clientTop * zoom);
        bottom = Math.min(
          bottom,
          clip.top + (ancestor.clientTop + ancestor.clientHeight) * zoom,
        );
      }
      ancestor = ancestor.parentElement;
    }
    return {
      visibleCssHeight: Math.max(0, bottom - top) / zoom,
      clientWidth: node.clientWidth,
      scrollWidth: node.scrollWidth,
      overflow: getComputedStyle(node).overflowY,
    };
  });
  expect(
    geometry.visibleCssHeight,
    JSON.stringify(geometry),
  ).toBeGreaterThanOrEqual(120);
  expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth + 1);
}
for (const viewport of [
  { width: 1440, height: 1000, zoom: 1 },
  { width: 390, height: 600, zoom: 1 },
  { width: 320, height: 400, zoom: 1 },
  { width: 1440, height: 1000, zoom: 2 },
])
  for (const theme of ["light", "dark"])
    test(`consent keyboard and bounds ${viewport.width}x${viewport.height} zoom${viewport.zoom} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await setup(page);
      if (viewport.zoom === 2)
        await page.evaluate(() => {
          document.documentElement.style.zoom = "2";
        });
      await page
        .getByRole("combobox", { name: "Appearance", exact: true })
        .selectOption(theme);
      await page
        .getByRole("button", {
          name: "Review product-edit consent",
          exact: true,
        })
        .click();
      const modal = page.getByRole("dialog", {
        name: "Review whole-product edit consent",
        exact: true,
      });
      await expectReadableStep(page);
      await modal
        .getByRole("button", { name: "Continue", exact: true })
        .scrollIntoViewIfNeeded();
      await modal
        .getByRole("button", { name: "Continue", exact: true })
        .click();
      await expectReadableStep(page);
      await modal
        .getByRole("button", { name: "Continue", exact: true })
        .scrollIntoViewIfNeeded();
      await modal
        .getByRole("button", { name: "Continue", exact: true })
        .click();
      await expectReadableStep(page);
      const ack = modal.getByRole("checkbox", {
        name: /I reviewed this person/,
      });
      await ack.scrollIntoViewIfNeeded();
      await ack.focus();
      await expect(ack).toBeFocused();
      await expect(ack).not.toBeChecked();
      await page.keyboard.press("Space");
      await expect(ack).toBeChecked();
      const measures = await modal.evaluate((node) => {
        const box = node.getBoundingClientRect(),
          scroll = node.querySelector(".wizard-field-scroll")!;
        return {
          top: box.top,
          left: box.left,
          right: box.right,
          bottom: box.bottom,
          width: scroll.clientWidth,
          scrollWidth: scroll.scrollWidth,
        };
      });
      expect(measures.top).toBeGreaterThanOrEqual(0);
      expect(measures.left).toBeGreaterThanOrEqual(0);
      expect(measures.right).toBeLessThanOrEqual(viewport.width);
      expect(measures.bottom).toBeLessThanOrEqual(viewport.height);
      expect(measures.scrollWidth).toBeLessThanOrEqual(measures.width + 1);
      const save = modal.getByRole("button", {
        name: "Record product-edit consent",
        exact: true,
      });
      await save.scrollIntoViewIfNeeded();
      await expect(save).toBeInViewport();
      const visible = await save.evaluate((node) => {
        const r = node.getBoundingClientRect();
        const owner = node.closest("dialog")!.getBoundingClientRect();
        return {
          top: r.top,
          bottom: r.bottom,
          ownerTop: owner.top,
          ownerBottom: owner.bottom,
          height: r.height,
          focused: document.activeElement === node,
        };
      });
      expect(visible.top).toBeGreaterThanOrEqual(visible.ownerTop);
      expect(visible.bottom).toBeLessThanOrEqual(visible.ownerBottom);
      await save.focus();
      await expect(save).toBeFocused();
      await page.screenshot({
        path: testInfo.outputPath(`consent-${viewport.width}-${theme}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(modal).not.toBeVisible();
    });

test("a recorded revocation preserves its original request and exact readback", async ({
  page,
}) => {
  const { state, panel } = await setup(page);
  await page
    .getByRole("button", { name: "Review product-edit consent", exact: true })
    .click();
  const modal = page.getByRole("dialog", {
    name: "Review whole-product edit consent",
    exact: true,
  });
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await modal
    .getByRole("checkbox", {
      name: /Allow this person to request signed whole-product edits/,
    })
    .uncheck();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await modal.getByRole("checkbox", { name: /I reviewed this person/ }).check();
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toContainText("revoked");
  expect(state.posts).toHaveLength(1);
  expect(state.posts[0].active).toBe(false);
});

test("a denied exact read clears private results and keeps the original identity", async ({
  page,
}) => {
  const { state, panel } = await setup(page);
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  state.exactStatus = 403;
  await panel
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(panel.getByRole("alert")).toContainText(
    "Private consent details have been cleared",
  );
  expect(state.posts).toHaveLength(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((key) =>
          key.startsWith("forge.scm-product-change-consent.v1:"),
        ).length,
    ),
  ).toBe(1);
});

test("an invalid original product-change reference performs no result request or write", async ({
  page,
}) => {
  const { state, panel } = await setup(page);
  let reads = 0;
  await page.route("**/scm-product-changes/**", (route) => {
    reads++;
    return route.fulfill({ status: 404, json: { detail: "Missing" } });
  });
  await panel
    .getByLabel("Original product-change request reference", { exact: true })
    .fill("../wrong");
  await panel
    .getByRole("button", {
      name: "Read original product-edit result",
      exact: true,
    })
    .click();
  await expect(panel.getByRole("alert")).toContainText("original 32-character");
  expect(reads).toBe(0);
  expect(state.posts).toHaveLength(0);
});

test("an original signed result follows its historical grant and mapping without writing", async ({
  page,
}) => {
  const { state, panel, binding } = await setup(page);
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Record product-edit consent", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact product-edit consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  const grant = state.current!;
  const value = structuredClone(packet.operation);
  value.project_id = binding.project_id;
  value.repository_id = binding.repository_id;
  value.native_user_id = binding.request.native_user_id;
  value.binding_request_id = binding.request.request_id;
  value.binding_digest = binding.digest;
  value.grant_request_id = grant.request.request_id;
  value.grant_digest = grant.digest;
  value.verified_reference = {
    ...binding.verified_reference,
    payload_digest: "c".repeat(64),
  };
  value.request.project_id = binding.project_id;
  value.request.expected_project_binding_digest =
    binding.project_binding_digest;
  value.request.expected_grant_request_id = grant.request.request_id;
  value.request.expected_grant_digest = grant.digest;
  value.request_digest = digest(value.request);
  const { digest: ignored, ...unsigned } = value;
  void ignored;
  value.digest = digest(unsigned);
  const reads: string[] = [];
  await page.route("**/scm-product-changes/requests/**", (route) => {
    reads.push(route.request().method());
    return route.fulfill({ json: value });
  });
  await panel
    .getByLabel("Original product-change request reference", { exact: true })
    .fill(value.request.change.request_id);
  await panel
    .getByRole("button", {
      name: "Read original product-edit result",
      exact: true,
    })
    .click();
  await expect(
    panel.getByRole("article", {
      name: "Exact signed product-edit result",
      exact: true,
    }),
  ).toContainText("Historical product revision 2");
  expect(reads).toEqual(["GET"]);
  expect(state.posts).toHaveLength(1);
});

test("two tabs admit one durable consent request before one POST", async ({
  page,
}) => {
  const first = await setup(page),
    secondPage = await page.context().newPage();
  try {
    const second = await setup(secondPage, false, true);
    const [a, b] = await Promise.all([review(page), review(secondPage)]);
    await Promise.all([
      a
        .getByRole("button", {
          name: "Record product-edit consent",
          exact: true,
        })
        .click(),
      b
        .getByRole("button", {
          name: "Record product-edit consent",
          exact: true,
        })
        .click(),
    ]);
    await expect
      .poll(() => first.state.posts.length + second.state.posts.length)
      .toBe(1);
    const identities = await page.evaluate(() =>
      Object.entries(localStorage).filter(([key]) =>
        key.startsWith("forge.scm-product-change-consent.v1:"),
      ),
    );
    expect(identities).toHaveLength(1);
    expect(first.state.posts.length + second.state.posts.length).toBe(1);
  } finally {
    await secondPage.close();
  }
});
