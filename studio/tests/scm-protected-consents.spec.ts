/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import packet from "./scm-delegation-contract-fixture.json" with { type: "json" };
import type { DelegationInput } from "../src/ScmDelegationData";
import {
  delegationScope,
  headFor,
  projectCut,
  receiptFor,
  setupDelegation,
} from "./scm-delegation-fixture";
import { captureWalkthrough } from "./walkthrough-capture";

type Consent = "delegation" | "resume" | "product-edit";
const producer = "studio/tests/scm-protected-consents.spec.ts";

async function existingConsent(
  page: Page,
  consent: Exclude<Consent, "delegation">,
) {
  const binding = receiptFor({
    ...packet.request,
    expected_project_binding_digest: projectCut,
  } as DelegationInput);
  const state = await setupDelegation(page, {
    heads: [headFor(binding)],
    initialReceipt: binding,
  });
  let writes = 0;
  const resource =
    consent === "resume" ? "scm-resume-grants" : "scm-product-change-grants";
  await page.route(`**/${resource}**`, async (route) => {
    const write = route.request().method() !== "GET";
    if (write) writes++;
    await route.fulfill({
      status: write ? 403 : 404,
      json: { detail: "No consent is recorded in this controlled guide." },
    });
  });
  await page
    .getByRole("button", { name: "Read current delegations", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Read exact revision 1", exact: true })
    .click();
  return { state, writes: () => writes };
}

async function boundedFrame(page: Page, dialog: Locator) {
  await expect(dialog.locator(".wizard-frame")).toHaveAttribute(
    "data-layout",
    "bounded",
  );
  const geometry = await dialog.evaluate((node) => {
    const bounds = node.getBoundingClientRect();
    const field = node.querySelector(".wizard-field-scroll")!;
    const actions = node.querySelector(".wizard-actions")!;
    const fieldBounds = field.getBoundingClientRect();
    const actionBounds = actions.getBoundingClientRect();
    const header = node
      .querySelector(".access-dialog-heading")!
      .getBoundingClientRect();
    const heading = node
      .querySelector(".wizard-heading")!
      .getBoundingClientRect();
    return {
      inside:
        bounds.top >= 0 &&
        bounds.bottom <= innerHeight &&
        bounds.left >= 0 &&
        bounds.right <= innerWidth,
      outerScroll: node.scrollTop,
      fields: field.clientHeight,
      nativeScroll: /auto|scroll/.test(getComputedStyle(field).overflowY),
      horizontal: field.scrollWidth <= field.clientWidth + 1,
      separated: fieldBounds.bottom <= actionBounds.top + 1,
      titles:
        header.top >= bounds.top &&
        header.bottom <= bounds.bottom &&
        heading.top >= bounds.top &&
        heading.bottom <= bounds.bottom,
      actions: [...actions.querySelectorAll("button")].every((button) => {
        const b = button.getBoundingClientRect();
        return (
          b.top >= bounds.top &&
          b.bottom <= bounds.bottom &&
          b.bottom <= innerHeight &&
          button.contains(
            document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2),
          )
        );
      }),
    };
  });
  expect(geometry.inside).toBe(true);
  expect(geometry.outerScroll).toBe(0);
  expect(geometry.fields).toBeGreaterThanOrEqual(64);
  expect(geometry.nativeScroll).toBe(true);
  expect(geometry.horizontal).toBe(true);
  expect(geometry.separated).toBe(true);
  expect(geometry.titles).toBe(true);
  expect(geometry.actions).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
}

async function keyboardFields(page: Page, dialog: Locator, end: boolean) {
  const fields = dialog.locator(".wizard-field-scroll");
  await fields.focus();
  if (
    await fields.evaluate((node) => node.scrollHeight > node.clientHeight + 1)
  ) {
    await page.keyboard.press("End");
    await expect
      .poll(() =>
        fields.evaluate((node) =>
          Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop),
        ),
      )
      .toBeLessThanOrEqual(2);
  }
  if (!end) {
    await page.keyboard.press("Home");
    await expect.poll(() => fields.evaluate((node) => node.scrollTop)).toBe(0);
  }
}

for (const consent of ["delegation", "resume", "product-edit"] as const)
  for (const device of ["desktop", "mobile"] as const)
    for (const theme of ["light", "dark"] as const)
      test(`guided protected consent frames ${consent} ${device} ${theme}`, async ({
        page,
      }, info) => {
        const viewport =
          device === "desktop"
            ? { width: 1440, height: 900 }
            : { width: 600, height: 390 };
        await page.setViewportSize(viewport);
        const setup =
          consent === "delegation"
            ? { state: await setupDelegation(page), writes: () => 0 }
            : await existingConsent(page, consent);
        await page
          .getByLabel("Appearance", { exact: true })
          .selectOption(theme);
        const frame = async (dialog: Locator, stage: string) => {
          await boundedFrame(page, dialog);
          expect(setup.state.posts).toHaveLength(0);
          expect(setup.writes()).toBe(0);
          await captureWalkthrough(page, stage, theme, device, {
            producer,
            boundary: "dialog",
            resetFields: false,
          });
          await boundedFrame(page, dialog);
          if (!process.env.UPDATE_DOC_SCREENSHOTS)
            await page.screenshot({ path: info.outputPath(`${stage}.png`) });
        };
        let dialog: Locator;
        if (consent === "delegation") {
          dialog = await delegationScope(page);
          await dialog
            .getByLabel("Find a registered person", { exact: true })
            .fill("Warehouse");
          await dialog
            .getByLabel("Find a registered person", { exact: true })
            .press("Enter");
          const person = dialog.getByRole("radio", {
            name: /Warehouse reviewer/,
          });
          await expect(person).toBeVisible();
          if (device === "mobile")
            await page.setViewportSize({ width: 320, height: 568 });
          await keyboardFields(page, dialog, false);
          await frame(dialog, "scm-protected-delegation-search");
          if (device === "mobile") await page.setViewportSize(viewport);
          await person.focus();
          await page.keyboard.press("Space");
          await expect(
            dialog.getByRole("group", {
              name: "Selected registered person",
              exact: true,
            }),
          ).toContainText("Warehouse reviewer");
          await dialog
            .getByRole("checkbox", { name: "Inventory API", exact: true })
            .check();
          await dialog
            .getByRole("checkbox", {
              name: "Answer agent questions",
              exact: true,
            })
            .check();
        } else {
          await page
            .getByRole("button", {
              name:
                consent === "resume"
                  ? "Review resume consent"
                  : "Review product-edit consent",
              exact: true,
            })
            .click();
          dialog = page.getByRole("dialog", {
            name:
              consent === "resume"
                ? "Review same-run resume consent"
                : "Review whole-product edit consent",
            exact: true,
          });
          await dialog
            .getByRole("button", { name: "Continue", exact: true })
            .click();
        }
        await keyboardFields(page, dialog, false);
        await frame(dialog, `scm-protected-${consent}-scope`);
        await dialog
          .getByRole("button", {
            name: consent === "delegation" ? "Review delegation" : "Continue",
            exact: true,
          })
          .click();
        const acknowledgement = dialog.getByRole("checkbox", {
          name:
            consent === "delegation"
              ? "I trust this webhook assertion."
              : /I reviewed this person/,
        });
        await acknowledgement.focus();
        await page.keyboard.press("Space");
        await expect(acknowledgement).toBeChecked();
        await keyboardFields(page, dialog, true);
        await frame(dialog, `scm-protected-${consent}-review`);
        await acknowledgement.focus();
        await page.keyboard.press("Tab");
        await expect(
          dialog.getByRole("button", {
            name: consent === "delegation" ? "Cancel" : "Back",
            exact: true,
          }),
        ).toBeFocused();
        await page.keyboard.press("Tab");
        await expect(dialog.locator(".wizard-actions .primary")).toBeFocused();
        await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
        expect(setup.state.posts).toHaveLength(0);
        expect(setup.writes()).toBe(0);
      });
