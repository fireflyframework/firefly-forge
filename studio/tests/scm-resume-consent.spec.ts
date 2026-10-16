/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import v1 from "./scm-delegation-contract-fixture.json" with { type: "json" };
import packet from "./scm-resume-contract-fixture.json" with { type: "json" };
import {
  setupDelegation,
  receiptFor,
  headFor,
  projectCut,
  digest,
} from "./scm-delegation-fixture";
import { navigate } from "./navigation";
import type { DelegationInput } from "../src/ScmDelegationData";
import type { ResumeInput, ResumeReceipt } from "../src/ScmResumeConsentData";

// Constructed, Python-model-shaped responses; these browser cases are not native/provider acceptance.
async function setup(page: Page, reader = false) {
  const binding = receiptFor({
    ...v1.request,
    expected_project_binding_digest: projectCut,
  } as DelegationInput);
  const v1state = await setupDelegation(page, {
    reader,
    heads: [headFor(binding)],
    initialReceipt: binding,
  });
  const state = {
    posts: [] as ResumeInput[],
    receipts: new Map<string, ResumeReceipt>(),
    current: null as ResumeReceipt | null,
    exactStatus: 200,
    postStatus: 201,
    loseResponse: false,
    markersAtPost: [] as unknown[][],
  };
  await page.route("**/scm-resume-grants**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (req.method() === "POST") {
      const body = req.postDataJSON() as ResumeInput;
      state.posts.push(body);
      state.markersAtPost.push(
        await page.evaluate(() =>
          Object.entries(localStorage)
            .filter(([key]) => key.startsWith("forge.scm-resume-consent.v2:"))
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
      const receipt = { ...rest, digest: digest(rest) } as ResumeReceipt;
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
    name: "Same-run resume consent",
    exact: true,
  });
  await expect(
    page
      .getByRole("region", { name: "Exact delegation receipt", exact: true })
      .getByRole("region", { name: "Same-run resume consent", exact: true }),
  ).toHaveCount(0);
  await expect(panel).toBeVisible();
  return { state, panel, v1state, binding };
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Review resume consent", exact: true })
    .click();
  const modal = page.getByRole("dialog", {
    name: "Review same-run resume consent",
    exact: true,
  });
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    modal.getByRole("checkbox", { name: "Inventory API", exact: true }),
  ).toBeChecked();
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
    .getByRole("button", { name: "Record resume consent", exact: true })
    .click();
  await expect(
    panel.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(v1state.posts).toHaveLength(0);
  expect(state.markersAtPost[0]).toHaveLength(1);
  expect(state.posts[0].allowed_actions).toEqual(["resume"]);
  expect(state.markersAtPost[0][0]).not.toHaveProperty("component_ids");
  await expect(
    panel.getByText(
      "Exact consent receipt confirmed. Saving consent does not resume a run.",
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
    .getByRole("button", { name: "Record resume consent", exact: true })
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
      name: "Exact resume consent receipt",
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
    .getByRole("button", { name: "Record resume consent", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "Review resume consent", exact: true }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  expect(
    await page.evaluate(
      () =>
        Object.keys(localStorage).filter((key) =>
          key.startsWith("forge.scm-resume-consent.v2:"),
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
    .getByRole("button", { name: "Record resume consent", exact: true })
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
      name: "Read current resume consent",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "Review resume consent", exact: true }),
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
    .getByRole("button", { name: "Record resume consent", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "cannot coordinate consent",
  );
  expect(state.posts).toHaveLength(0);
});
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
      const modal = await review(page);
      const ack = modal.getByRole("checkbox", {
        name: /I reviewed this person/,
      });
      await ack.focus();
      await page.keyboard.press("Space");
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
      await modal
        .getByRole("button", {
          name: "Record resume consent",
          exact: true,
        })
        .scrollIntoViewIfNeeded();
      await expect(
        modal.getByRole("button", {
          name: "Record resume consent",
          exact: true,
        }),
      ).toBeInViewport();
      await page.screenshot({
        path: testInfo.outputPath(`consent-${viewport.width}-${theme}.png`),
      });
      await page.keyboard.press("Escape");
      await expect(modal).not.toBeVisible();
    });

async function readingArea(content: Locator) {
  return content.evaluate((node) => {
    const zoom =
      Number.parseFloat(getComputedStyle(document.documentElement).zoom) || 1;
    let top = 0,
      bottom = innerHeight,
      left = 0,
      right = innerWidth;
    let nearest: { height: number; width: number; scrollWidth: number } | null =
      null;
    for (
      let parent = node.parentElement;
      parent;
      parent = parent.parentElement
    ) {
      const style = getComputedStyle(parent),
        box = parent.getBoundingClientRect();
      if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
        const start = box.top + parent.clientTop * zoom;
        top = Math.max(top, start);
        bottom = Math.min(bottom, start + parent.clientHeight * zoom);
        nearest ??= {
          height: parent.clientHeight,
          width: parent.clientWidth,
          scrollWidth: parent.scrollWidth,
        };
      }
      if (/auto|scroll|hidden|clip/.test(style.overflowX)) {
        const start = box.left + parent.clientLeft * zoom;
        left = Math.max(left, start);
        right = Math.min(right, start + parent.clientWidth * zoom);
      }
      if (parent instanceof HTMLDialogElement) break;
    }
    const target = node.getBoundingClientRect();
    const dialog = node.closest("dialog")!;
    const describe = (element: Element) => {
      const box = element.getBoundingClientRect(),
        style = getComputedStyle(element);
      return {
        tag: element.tagName,
        className: element.className,
        rect: {
          top: box.top,
          bottom: box.bottom,
          left: box.left,
          right: box.right,
        },
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
        gridTemplateColumns: style.gridTemplateColumns,
        padding: style.padding,
        overflow: style.overflow,
      };
    };
    return {
      diagnostics: {
        zoom,
        clip: { top, bottom, left, right },
        target: describe(node),
        dialog: describe(dialog),
        actions: describe(dialog.querySelector(".wizard-actions")!),
        steps: describe(dialog.querySelector(".wizard-steps")!),
        horizontalOverflow: Array.from(dialog.querySelectorAll("*"))
          .filter((element) => {
            const box = element.getBoundingClientRect();
            return box.left < left - 1 || box.right > right + 1;
          })
          .slice(0, 30)
          .map(describe),
        stylesheetOrder: Array.from(
          document.querySelectorAll("style[data-vite-dev-id]"),
        )
          .map((element) => element.getAttribute("data-vite-dev-id"))
          .filter((path) =>
            /Scm(Delegation|ResumeConsent)\.css$/.test(path ?? ""),
          ),
      },
      usefulHeight: nearest
        ? Math.min(nearest.height, (bottom - top) / zoom)
        : 0,
      width: nearest?.width ?? 0,
      scrollWidth: nearest?.scrollWidth ?? 0,
      fullyVisible:
        target.top >= top &&
        target.bottom <= bottom &&
        target.left >= left &&
        target.right <= right,
      hit: node.contains(
        document.elementFromPoint(
          target.x + target.width / 2,
          target.y + target.height / 2,
        ),
      ),
    };
  });
}

for (const viewport of [
  { width: 320, height: 400, zoom: 1 },
  { width: 320, height: 400, zoom: 2 },
  { width: 1440, height: 1000, zoom: 2 },
])
  for (const theme of ["light", "dark"])
    test(`consent has a readable scrolling viewport on every step ${viewport.width}x${viewport.height} zoom${viewport.zoom} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      const { state } = await setup(page);
      await page.evaluate((zoom) => {
        document.documentElement.style.zoom = String(zoom);
      }, viewport.zoom);
      await page
        .getByRole("combobox", { name: "Appearance", exact: true })
        .selectOption(theme);
      await page
        .getByRole("button", { name: "Review resume consent", exact: true })
        .click();
      const modal = page.getByRole("dialog", {
        name: "Review same-run resume consent",
        exact: true,
      });
      for (let step = 0; step < 3; step++) {
        const content =
          step === 0
            ? modal.getByText(/They may request the same paused run/)
            : step === 1
              ? modal
                  .getByRole("checkbox", { name: "Inventory API", exact: true })
                  .locator("..")
              : modal.getByText(/Consent adds no membership/);
        await content.scrollIntoViewIfNeeded();
        const area = await readingArea(content);
        await testInfo.attach(`reading-area-step-${step}`, {
          body: JSON.stringify(area),
          contentType: "application/json",
        });
        expect(area.usefulHeight).toBeGreaterThanOrEqual(120);
        expect(area.scrollWidth).toBeLessThanOrEqual(area.width + 1);
        if (step === 2) {
          const ack = modal.getByRole("checkbox", {
            name: /I reviewed this person/,
          });
          await ack.focus();
          await expect(ack).toBeFocused();
          const focus = await readingArea(ack);
          expect(focus.fullyVisible).toBe(true);
          expect(focus.hit).toBe(true);
          await page.keyboard.press("Space");
          await expect(ack).toBeChecked();
        }
        const action = modal.getByRole("button", {
          name: step === 2 ? "Record resume consent" : "Continue",
          exact: true,
        });
        await action.scrollIntoViewIfNeeded();
        await action.focus();
        await expect(action).toBeFocused();
        await expect(action).toBeInViewport();
        const focus = await readingArea(action);
        await testInfo.attach(`action-focus-step-${step}`, {
          body: JSON.stringify(focus),
          contentType: "application/json",
        });
        expect(focus.fullyVisible).toBe(true);
        expect(focus.hit).toBe(true);
        await page.screenshot({
          path: testInfo.outputPath(`consent-reading-step-${step}.png`),
        });
        expect(state.posts).toHaveLength(0);
        if (step < 2) await page.keyboard.press("Enter");
      }
      await page.keyboard.press("Escape");
      await expect(modal).not.toBeVisible();
      await expect(
        page.getByRole("button", {
          name: "Review resume consent",
          exact: true,
        }),
      ).toBeFocused();
      expect(state.posts).toHaveLength(0);
    });
