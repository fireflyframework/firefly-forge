/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";

async function fixture(page: Page, modal = "") {
  await page.route("**/api/v1/**", (route) =>
    route.fulfill({ status: 401, json: { detail: "Layout fixture" } }),
  );
  await page.goto("/");
  await page.evaluate(async (modal) => {
    const { default: React } =
      await import("/node_modules/.vite/deps/react.js");
    const {
      default: { createRoot },
    } = await import("/node_modules/.vite/deps/react-dom_client.js");
    const { Wizard } = await import("/src/Wizard.tsx");
    const h = React.createElement;
    const root = document.createElement("div");
    root.id = "root";
    document.body.replaceChildren(root);
    function Fixture() {
      const [notice, setNotice] = React.useState(false);
      const [step, setStep] = React.useState(0);
      const wizard = h(
        Wizard,
        {
          title: step ? "Review the retained draft" : "Define the service",
          description: "Enter the customer behavior before reviewing it.",
          steps: ["Details", "Review"],
          active: step,
          onStep: setStep,
        },
        h(
          "form",
          { onSubmit: (event: Event) => event.preventDefault() },
          ...Array.from({ length: 14 }, (_, index) =>
            h(
              "div",
              { className: "field", key: index },
              h("label", { htmlFor: `detail-${index}` }, `Detail ${index + 1}`),
              h("input", { id: `detail-${index}` }),
            ),
          ),
          h(
            "div",
            { className: "wizard-actions" },
            h(
              "button",
              {
                type: "button",
                onClick: () => setStep(1),
                className: "primary",
              },
              "Continue",
            ),
          ),
        ),
      );
      const dialog = h(
        "dialog",
        {
          className: modal,
          ref: (element: HTMLDialogElement | null) => element?.showModal(),
        },
        h(
          "header",
          null,
          h(
            "h2",
            null,
            "Inspect the existing product and its customer processes",
          ),
        ),
        wizard,
      );
      return h(
        "div",
        { className: "app" },
        h("aside", { className: "sidebar" }),
        h(
          "div",
          { className: "workspace-main" },
          h("header", { className: "topbar" }, "Firefly workspace"),
          h(
            "main",
            null,
            h("button", { onClick: () => setNotice(!notice) }, "Toggle notice"),
            notice &&
              h(
                "div",
                { className: "alert", style: { minHeight: 180 } },
                "Connection needs attention",
              ),
            !modal &&
              h("section", { "data-testid": "application-frame" }, wizard),
            modal && dialog,
          ),
          h("footer", null, "Firefly Software Foundation"),
        ),
      );
    }
    createRoot(root).render(h(Fixture));
  }, modal);
}

test("application frames use the available wide grid", async ({ page }) => {
  await page.setViewportSize({ width: 2560, height: 1080 });
  await fixture(page);
  const geometry = await page
    .getByTestId("application-frame")
    .evaluate((frame) => {
      const main = frame.closest("main")!;
      const style = getComputedStyle(main);
      return {
        width: frame.getBoundingClientRect().width,
        available:
          main.clientWidth -
          parseFloat(style.paddingLeft) -
          parseFloat(style.paddingRight),
      };
    });
  expect(geometry.width).toBeGreaterThanOrEqual(geometry.available - 1);
});

test("a new notice reflows the wizard without losing its draft or focus", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await fixture(page);
  await page
    .getByLabel("Detail 1", { exact: true })
    .fill("Inventory reservation");
  await page.getByRole("button", { name: "Toggle notice" }).click();
  await page.getByLabel("Detail 1", { exact: true }).focus();
  await expect
    .poll(() =>
      page.locator(".wizard-frame").evaluate((frame) => {
        const main = frame.closest("main")!;
        return (
          frame.getBoundingClientRect().bottom -
          (main.getBoundingClientRect().bottom -
            parseFloat(getComputedStyle(main).paddingBottom))
        );
      }),
    )
    .toBeLessThanOrEqual(1);
  await expect(page.getByLabel("Detail 1", { exact: true })).toHaveValue(
    "Inventory reservation",
  );
  await expect(page.getByLabel("Detail 1", { exact: true })).toBeFocused();
});

for (const modal of [
  "feature-dialog",
  "feature-dialog source-analysis-dialog",
]) {
  test(`${modal} retains visible actions in a short viewport`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: 900, height: 480 });
    await fixture(page, modal);
    const dialog = page.getByRole("dialog");
    const action = dialog.getByRole("button", {
      name: "Continue",
      exact: true,
    });
    await expect
      .poll(() =>
        action.evaluate((button) => {
          const dialog = button.closest("dialog")!;
          const bounds = dialog.getBoundingClientRect();
          const box = button.getBoundingClientRect();
          return (
            box.bottom <=
              bounds.bottom -
                parseFloat(getComputedStyle(dialog).paddingBottom) &&
            box.top >= bounds.top
          );
        }),
      )
      .toBe(true);
    await dialog
      .getByLabel("Detail 1", { exact: true })
      .fill("Keep this customer behavior");
    await dialog.getByLabel("Detail 14", { exact: true }).focus();
    await expect(
      dialog.getByLabel("Detail 14", { exact: true }),
    ).toBeInViewport();
    await expect(dialog.locator(":scope > header")).toBeInViewport();
    await action.click();
    await expect(
      dialog.getByRole("heading", {
        name: "Review the retained draft",
        exact: true,
      }),
    ).toBeFocused();
    await expect(dialog.getByLabel("Detail 1", { exact: true })).toHaveValue(
      "Keep this customer behavior",
    );
    await expect
      .poll(() =>
        dialog
          .locator(".wizard-field-scroll")
          .evaluate((element) => element.scrollTop),
      )
      .toBe(0);
    await dialog.evaluate((element: HTMLDialogElement) => element.close());
    await expect(page.locator("dialog")).toBeHidden();
  });
}
