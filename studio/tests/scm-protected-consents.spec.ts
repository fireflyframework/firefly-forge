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

async function responsiveStep(page: Page, dialog: Locator) {
  await expect(dialog).toHaveAttribute("data-fit", /^(bounded|flow)$/);
  const flow = (await dialog.getAttribute("data-fit")) === "flow";
  await expect(dialog.locator(".wizard-frame")).toHaveAttribute(
    "data-layout",
    flow ? "flow" : "bounded",
  );
  if (!flow) {
    await boundedFrame(page, dialog);
    await keyboardFields(page, dialog, false);
    return;
  }
  const budget = await dialog.evaluate((node) => {
    const probe = node.cloneNode(true) as HTMLDialogElement;
    probe.dataset.fit = "bounded";
    probe.inert = true;
    probe.setAttribute("aria-hidden", "true");
    probe.style.visibility = "hidden";
    // A nonmodal clone does not inherit the browser's :modal maximum width.
    probe.style.width = getComputedStyle(node).width;
    probe.style.maxWidth = "none";
    probe.querySelector<HTMLElement>(".wizard-frame")!.dataset.layout =
      "bounded";
    node.parentElement!.append(probe);
    try {
      return {
        fields: probe.querySelector<HTMLElement>(".wizard-field-scroll")!
          .clientHeight,
        width: probe.getBoundingClientRect().width,
        actualWidth: node.getBoundingClientRect().width,
      };
    } finally {
      probe.remove();
    }
  });
  expect(budget.width).toBeCloseTo(budget.actualWidth, 1);
  expect(budget.fields, JSON.stringify(budget)).toBeLessThan(64);
  const fields = dialog.locator(".wizard-field-scroll");
  await expect(fields).not.toHaveAttribute("role", "region");
  const geometry = await dialog.evaluate((node) => ({
    horizontal: node.scrollWidth <= node.clientWidth + 1,
    overflowing: [...node.querySelectorAll("*")]
      .filter((element) => {
        const bounds = element.getBoundingClientRect(),
          owner = node.getBoundingClientRect();
        return bounds.left < owner.left || bounds.right > owner.right;
      })
      .slice(0, 15)
      .map((element) => ({
        tag: element.tagName,
        className: element.className,
        text: element.textContent?.slice(0, 80),
        width: element.getBoundingClientRect().width,
        scroll: element.scrollWidth,
        client: element.clientWidth,
      })),
    native: /auto|scroll/.test(getComputedStyle(node).overflowY),
    inner: [
      ...node.querySelectorAll(
        ".wizard-body,.wizard-content,.wizard-form,.wizard-field-scroll,.form-scroll-region",
      ),
    ].every(
      (element) =>
        !/auto|scroll|hidden|clip/.test(getComputedStyle(element).overflowY),
    ),
  }));
  expect(geometry, JSON.stringify(geometry)).toMatchObject({
    horizontal: true,
    native: true,
    inner: true,
  });
  await dialog.focus();
  await page.keyboard.press("Home");
  await expect.poll(() => dialog.evaluate((node) => node.scrollTop)).toBe(0);
  const close = dialog.getByRole("button", { name: /^Close/ });
  await close.focus();
  await expect(close).toBeFocused();
  await expect(close).toBeInViewport();
  await expect
    .poll(() =>
      close.evaluate((node) => {
        const b = node.getBoundingClientRect();
        return node.contains(
          document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2),
        );
      }),
    )
    .toBe(true);
  await dialog.focus();
  await page.keyboard.press("End");
  await expect
    .poll(() =>
      dialog.evaluate((node) =>
        Math.abs(node.scrollHeight - node.clientHeight - node.scrollTop),
      ),
    )
    .toBeLessThanOrEqual(2);
  for (const button of await dialog.locator(".wizard-actions button").all()) {
    if (!(await button.isDisabled())) {
      await button.focus();
      await expect(button).toBeFocused();
    }
    await expect(button).toBeInViewport();
    expect(
      await button.evaluate((node) => {
        const b = node.getBoundingClientRect();
        const bounds = node.closest("dialog")!.getBoundingClientRect();
        return (
          b.top >= Math.max(0, bounds.top) &&
          b.bottom <= Math.min(innerHeight, bounds.bottom) &&
          node.contains(
            document.elementFromPoint(b.x + b.width / 2, b.y + b.height / 2),
          )
        );
      }),
    ).toBe(true);
  }
}

async function responsiveReviewWithZoom(
  page: Page,
  consent: Consent,
  zoom: number,
  theme: string,
) {
  const setup =
    consent === "delegation"
      ? { state: await setupDelegation(page), writes: () => 0 }
      : await existingConsent(page, consent);
  await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
  await page.evaluate(
    (value) => (document.documentElement.style.zoom = String(value)),
    zoom,
  );
  let dialog: Locator;
  if (consent === "delegation") {
    dialog = await delegationScope(page);
    await dialog
      .getByLabel("Find a registered person", { exact: true })
      .fill("Warehouse");
    await dialog
      .getByLabel("Find a registered person", { exact: true })
      .press("Enter");
    await dialog.getByRole("radio", { name: /Warehouse reviewer/ }).click();
    await dialog
      .getByRole("checkbox", { name: "Inventory API", exact: true })
      .check();
    await dialog
      .getByRole("checkbox", { name: "Answer agent questions", exact: true })
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
    await responsiveStep(page, dialog);
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  }
  return { dialog, setup };
}

for (const consent of ["delegation", "resume", "product-edit"] as const)
  for (const viewport of [
    { width: 320, height: 568, zoom: 1 },
    { width: 1440, height: 1000, zoom: 2 },
    { width: 320, height: 400, zoom: 1 },
  ])
    for (const theme of ["light", "dark"] as const)
      test(`protected consent responsive fit ${consent} ${viewport.width}x${viewport.height} zoom${viewport.zoom} ${theme}`, async ({
        page,
      }) => {
        await page.setViewportSize(viewport);
        const { dialog, setup } = await responsiveReviewWithZoom(
          page,
          consent,
          viewport.zoom,
          theme,
        );
        await responsiveStep(page, dialog);
        if (viewport.height !== 400)
          await expect(dialog).toHaveAttribute("data-fit", "bounded");
        await dialog
          .getByRole("button", {
            name: consent === "delegation" ? "Review delegation" : "Continue",
            exact: true,
          })
          .click();
        await responsiveStep(page, dialog);
        if (viewport.height === 400 && consent !== "delegation")
          await expect(dialog).toHaveAttribute("data-fit", "flow");
        const ack = dialog.getByRole("checkbox", {
          name:
            consent === "delegation"
              ? "I trust this webhook assertion."
              : /I reviewed this person/,
        });
        await ack.focus();
        await expect(ack).toBeInViewport();
        await page.keyboard.press("Space");
        await expect(ack).toBeChecked();
        await responsiveStep(page, dialog);
        await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
        expect(setup.state.posts).toHaveLength(0);
        expect(setup.writes()).toBe(0);
      });

for (const consent of ["delegation", "resume", "product-edit"] as const)
  for (const theme of ["light", "dark"] as const)
    test(`guided protected consent flow frames ${consent} mobile ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width: 320, height: 400 });
      const { dialog, setup } = await responsiveReviewWithZoom(
        page,
        consent,
        2,
        theme,
      );
      for (const step of ["scope", "review"] as const) {
        await expect(dialog).toHaveAttribute("data-fit", "flow");
        if (step === "review") {
          const ack = dialog.getByRole("checkbox", {
            name:
              consent === "delegation"
                ? "I trust this webhook assertion."
                : /I reviewed this person/,
          });
          await ack.focus();
          await expect(ack).toBeInViewport();
          await page.keyboard.press("Space");
          await expect(ack).toBeChecked();
        }
        await responsiveStep(page, dialog);
        expect(setup.state.posts).toHaveLength(0);
        expect(setup.writes()).toBe(0);
        const stage = `scm-protected-${consent}-flow-${step}`;
        await captureWalkthrough(page, stage, theme, "mobile", {
          producer,
          boundary: "dialog",
          resetFields: false,
        });
        await responsiveStep(page, dialog);
        if (!process.env.UPDATE_DOC_SCREENSHOTS)
          await page.screenshot({ path: info.outputPath(`${stage}.png`) });
        if (step === "scope")
          await dialog
            .getByRole("button", {
              name: consent === "delegation" ? "Review delegation" : "Continue",
              exact: true,
            })
            .click();
      }
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      expect(setup.state.posts).toHaveLength(0);
      expect(setup.writes()).toBe(0);
    });

test("protected consent fit remeasures committed content after viewport, step and zoom changes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 400 });
  const { dialog, setup } = await responsiveReviewWithZoom(
    page,
    "resume",
    1,
    "light",
  );
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  await responsiveStep(page, dialog);
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  await page.setViewportSize({ width: 600, height: 390 });
  await expect(dialog).toHaveAttribute("data-fit", "bounded");
  await responsiveStep(page, dialog);
  await page.evaluate(() => (document.documentElement.style.zoom = "2"));
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  await responsiveStep(page, dialog);
  await page.evaluate(() => (document.documentElement.style.zoom = "1"));
  await expect(dialog).toHaveAttribute("data-fit", "bounded");
  await responsiveStep(page, dialog);
  await page.setViewportSize({ width: 320, height: 400 });
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(dialog).toHaveAttribute("data-fit", "bounded");
  await responsiveStep(page, dialog);
  expect(setup.state.posts).toHaveLength(0);
  expect(setup.writes()).toBe(0);
});

test("protected consent preserves input identity caret and active composition across fit changes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 600, height: 390 });
  const state = await setupDelegation(page);
  const dialog = await delegationScope(page);
  const input = dialog.getByLabel("Find a registered person", { exact: true });
  await input.fill("Warehouse");
  await input.focus();
  await input.evaluate((node) => node.setSelectionRange(2, 6));
  const original = await input.elementHandle();
  await page.setViewportSize({ width: 320, height: 400 });
  await page.evaluate(() => (document.documentElement.style.zoom = "2"));
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  expect(await input.evaluate((node, prior) => node === prior, original)).toBe(
    true,
  );
  await expect(input).toBeFocused();
  expect(
    await input.evaluate((node) => [node.selectionStart, node.selectionEnd]),
  ).toEqual([2, 6]);
  await page.evaluate(() => (document.documentElement.style.zoom = "1"));
  await page.setViewportSize({ width: 600, height: 390 });
  await expect(dialog).toHaveAttribute("data-fit", "bounded");
  await expect(input).toBeFocused();
  expect(
    await input.evaluate((node) => [node.selectionStart, node.selectionEnd]),
  ).toEqual([2, 6]);
  await input.fill("");
  await input.evaluate((node) => {
    const field = node as HTMLInputElement & { composition: string[] };
    field.composition = [];
    for (const type of [
      "compositionstart",
      "compositionupdate",
      "compositionend",
    ])
      field.addEventListener(type, () => field.composition.push(type));
  });
  const session = await page.context().newCDPSession(page);
  await session.send("Input.imeSetComposition", {
    text: "倉",
    selectionStart: 1,
    selectionEnd: 1,
  });
  await expect(input).toHaveValue("倉");
  await page.setViewportSize({ width: 320, height: 400 });
  await page.evaluate(() => (document.documentElement.style.zoom = "2"));
  await expect(dialog).toHaveAttribute("data-fit", "flow");
  expect(await input.evaluate((node, prior) => node === prior, original)).toBe(
    true,
  );
  await expect(input).toBeFocused();
  expect(
    await input.evaluate(
      (node) =>
        (node as HTMLInputElement & { composition: string[] }).composition,
    ),
  ).not.toContain("compositionend");
  await session.send("Input.imeSetComposition", {
    text: "倉庫",
    selectionStart: 2,
    selectionEnd: 2,
  });
  await expect(input).toHaveValue("倉庫");
  await session.send("Input.insertText", { text: "倉庫" });
  const events = await input.evaluate(
    (node) =>
      (node as HTMLInputElement & { composition: string[] }).composition,
  );
  expect(events.filter((type) => type === "compositionstart")).toHaveLength(1);
  expect(events.filter((type) => type === "compositionend")).toHaveLength(1);
  expect(state.posts).toHaveLength(0);
  await session.detach();
});
