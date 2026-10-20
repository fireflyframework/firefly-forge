/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import {
  setup,
  open,
  mitigation,
} from "./provider-recovery-allowance-browser-fixture";
import { captureWalkthrough as capture } from "./walkthrough-capture";

// Controlled HTTP fixtures illustrate the operator journey; they do not run a CLI.
const producer = "studio/tests/docs-provider-allowance.spec.ts";
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"]) {
    test(`actual fixture final allowance frames ${device} ${theme}`, async ({
      page,
    }, info) => {
      async function frame(
        stage: string,
        options: Parameters<typeof capture>[4],
      ) {
        await capture(page, stage, theme, device, options);
        if (!process.env.UPDATE_DOC_SCREENSHOTS)
          await page.screenshot({ path: info.outputPath(stage + ".png") });
      }
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await open(page);
      const options = { producer, boundary: "dialog" as const };
      await frame("provider-allowance-failure", options);
      await mitigation(page);
      await frame("provider-allowance-mitigation", options);
      await page
        .getByRole("button", { name: "Review reservation", exact: true })
        .click();
      await page
        .getByLabel(
          "I authorize this one final reservation and understand it cannot be renewed.",
          { exact: true },
        )
        .check();
      await frame("provider-allowance-review", options);
      await page
        .getByRole("button", { name: "Reserve final allowance", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Final allowance reserved",
          exact: true,
        }),
      ).toBeVisible();
      await frame("provider-allowance-reserved", {
        producer,
        actionSelector: ".provider-allowance-result button",
      });
    });
  }
