/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { prepare, open } from "./coding-check-recovery-browser-fixture";
import { captureWalkthrough as capture } from "./walkthrough-capture";

// Controlled HTTP walkthrough only. No native grant, command, provider or QA result is implied.
const producer = "studio/tests/docs-coding-check-recovery.spec.ts";
for (const device of ["desktop", "mobile"])
  for (const theme of ["light", "dark"])
    test(`actual fixture configured-check recovery frames ${device} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(
        device === "desktop"
          ? { width: 1440, height: 900 }
          : { width: 390, height: 844 },
      );
      const state = await prepare(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      const options = {
        producer,
        boundary: "dialog" as const,
        framing: "initial" as const,
      };
      async function frame(stage: string, actionSelector?: string) {
        await capture(page, stage, theme, device, {
          ...options,
          actionSelector,
        });
        if (!process.env.UPDATE_DOC_SCREENSHOTS)
          await page.screenshot({ path: info.outputPath(stage + ".png") });
      }
      await open(page);
      await frame("coding-check-scope");
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .getByLabel("Reason for granting check access")
        .fill(
          "Grant these retained checks for the reviewed recovery, using the displayed runner and command budget.",
        );
      await frame("coding-check-grant");
      await page
        .getByRole("button", { name: "Grant selected checks", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Review the recovery mitigation",
          exact: true,
        }),
      ).toBeVisible();
      await page
        .getByLabel("Mitigation reviewed", { exact: true })
        .fill(
          "Reviewed the provider mitigation and the retained fourth-attempt process receipt.",
        );
      await page
        .getByLabel("Mitigation evidence SHA-256", { exact: true })
        .fill("e".repeat(64));
      await page
        .getByLabel("Adopt the reviewed current CLI for this final recovery.", {
          exact: true,
        })
        .check();
      await frame("coding-check-mitigation");
      await page
        .getByRole("button", { name: "Review final recovery", exact: true })
        .click();
      await page.getByLabel(/I understand this spends/).check();
      await frame("coding-check-review");
      await page
        .getByRole("button", {
          name: "Reserve final attempt",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("dialog").getByRole("heading", {
          name: "Final recovery reserved",
          exact: true,
        }),
      ).toBeVisible();
      await frame(
        "coding-check-reserved",
        "dialog .provider-allowance-result button",
      );
      expect(state.grantPosts).toBe(1);
      expect(state.allowancePosts).toBe(1);
    });
