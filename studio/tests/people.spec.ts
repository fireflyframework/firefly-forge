/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { expectProgressFits } from "./wizard-progress";

for (const configured of [false, true]) {
  test(`people setup respects directory availability and explicit actions (${configured})`, async ({
    page,
  }) => {
    const user = "a".repeat(32),
      writes: { path: string; body: Record<string, unknown> }[] = [];
    let created = false;
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const path = new URL(route.request().url()).pathname;
      if (["POST", "PUT"].includes(route.request().method())) {
        const body = route.request().postDataJSON();
        writes.push({ path, body });
        if (path.endsWith("/identity/users")) {
          created = true;
          return route.fulfill({
            status: 202,
            json: {
              id: "b".repeat(32),
              kind: "provision",
              status: "completed",
              user_id: user,
              provider_created: true,
            },
          });
        }
        if (path.endsWith("/invite"))
          return route.fulfill({
            status: 202,
            json: {
              id: "c".repeat(32),
              kind: "invite",
              status: "completed",
              invitation_status: "provider_accepted",
              mail_delivery_verified: false,
            },
          });
        return route.fulfill({ json: {} });
      }
      return route.fulfill({
        json: path.endsWith("/identity/administration")
          ? {
              provider: "keycloak",
              configured,
              provisioning_verified: false,
              mail_delivery_verified: false,
            }
          : path.endsWith("/identity/users")
            ? created
              ? [
                  {
                    id: user,
                    display_name: "Warehouse reviewer",
                    email: null,
                    contact_email: "reviewer@example.test",
                    email_verified: false,
                    enabled: true,
                  },
                ]
              : []
            : path.endsWith("/health")
              ? { execution_ready: true }
              : path.endsWith("/profiles")
                ? {}
                : [],
      });
    });
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-bootstrap-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await page.setViewportSize({ width: 390, height: 844 });
    await navigate(page, "People");
    const add = page.getByRole("button", { name: "Add user", exact: true });
    if (!configured) {
      await expect(add).toBeDisabled();
      expect(writes).toEqual([]);
      return;
    }
    await add.click();
    const dialog = page.getByRole("dialog", { name: "Add organization user" });
    await page.setViewportSize({ width: 320, height: 568 });
    const progress = dialog.getByRole("list", { name: "Setup progress" });
    await expectProgressFits(progress, "Person");
    await page
      .getByLabel("Sign-in name", { exact: true })
      .fill("warehouse-reviewer");
    await page
      .getByLabel("Email · needed for an invitation", { exact: true })
      .fill("reviewer@example.test");
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await expectProgressFits(progress, "Connection");
    await dialog.getByRole("button", { name: "Back", exact: true }).click();
    await expect(page.getByLabel("Sign-in name", { exact: true })).toHaveValue(
      "warehouse-reviewer",
    );
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await expectProgressFits(progress, "Review");
    const fields = dialog.locator(".wizard-field-scroll");
    await fields.hover();
    await page.mouse.wheel(0, 600);
    await expect
      .poll(async () => {
        const pane = await fields.boundingBox();
        const checkbox = await page
          .getByLabel("I reviewed this person and the account to create.", {
            exact: true,
          })
          .boundingBox();
        return {
          pane,
          checkbox,
          fits:
            !!pane &&
            !!checkbox &&
            checkbox.y >= pane.y &&
            checkbox.y + checkbox.height <= pane.y + pane.height,
        };
      })
      .toMatchObject({ fits: true });
    await page
      .getByLabel("I reviewed this person and the account to create.", {
        exact: true,
      })
      .check();
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await expectProgressFits(progress, "Create");
    await expect(
      dialog.getByText(
        "Creating the account records the directory result. Invitation and project access are separate next actions.",
        { exact: true },
      ),
    ).toBeVisible();
    await page.setViewportSize({ width: 390, height: 844 });
    const create = dialog.getByRole("button", {
      name: "Create account",
      exact: true,
    });
    const box = await create.boundingBox();
    expect(box!.y + box!.height).toBeLessThan(844);
    expect(writes).toEqual([]);
    await create.click();
    await expect(
      page.getByRole("heading", { name: "Account created", exact: true }),
    ).toBeVisible();
    expect(writes).toHaveLength(1);
    expect(writes[0].body).toMatchObject({
      username: "warehouse-reviewer",
      email: "reviewer@example.test",
      first_name: null,
      last_name: null,
    });
    expect(writes[0].body.request_id).toMatch(/^[a-f0-9]{32}$/);
    await expect(
      page.getByRole("button", { name: "Invite", exact: true }),
    ).toBeEnabled();
    await page.getByRole("button", { name: "Invite", exact: true }).click();
    await expect(
      page.getByRole("dialog", { name: "Invite this person", exact: true }),
    ).toBeVisible();
    expect(writes).toHaveLength(1);
    await page
      .getByRole("button", { name: "Invite this person", exact: true })
      .click();
    await expect(
      page.getByText(
        "Your identity provider accepted the invitation request. Email delivery has not been verified.",
        { exact: true },
      ),
    ).toBeVisible();
    expect(writes).toHaveLength(2);
    expect(writes[1].path).toBe(`/api/v1/identity/users/${user}/invite`);
  });
}

for (const rejected of [true, false]) {
  test(`account creation ${rejected ? "allows correction after known validation rejection" : "does not repeat an uncertain remote admission"}`, async ({
    page,
  }) => {
    let attempts = 0;
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const path = new URL(route.request().url()).pathname;
      if (
        path.endsWith("/identity/users") &&
        route.request().method() === "POST"
      ) {
        attempts++;
        if (!rejected) return route.abort("failed");
        return route.fulfill({
          status: 422,
          json: {
            detail: [
              {
                loc: ["body", "username"],
                msg: "Choose another sign-in name",
                input: "must-not-be-displayed",
              },
            ],
          },
        });
      }
      return route.fulfill({
        json: path.endsWith("/identity/administration")
          ? { provider: "keycloak", configured: true }
          : path.endsWith("/health")
            ? { execution_ready: true }
            : path.endsWith("/profiles")
              ? {}
              : [],
      });
    });
    await page.goto("/");
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-bootstrap-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await navigate(page, "People");
    await page.getByRole("button", { name: "Add user", exact: true }).click();
    const dialog = page.getByRole("dialog", {
      name: "Add organization user",
      exact: true,
    });
    await page
      .getByLabel("Sign-in name", { exact: true })
      .fill("warehouse-reviewer");
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await page
      .getByLabel("I reviewed this person and the account to create.", {
        exact: true,
      })
      .check();
    await dialog.getByRole("button", { name: "Continue", exact: true }).click();
    await dialog
      .getByRole("button", { name: "Create account", exact: true })
      .click();
    await expect(dialog.getByRole("alert")).toBeVisible();
    expect(attempts).toBe(1);
    if (rejected) {
      await expect(
        page.getByLabel("Sign-in name", { exact: true }),
      ).toHaveValue("warehouse-reviewer");
      await expect(dialog.getByRole("alert")).toContainText(
        "Choose another sign-in name",
      );
      await expect(dialog.getByRole("alert")).not.toContainText(
        "must-not-be-displayed",
      );
      await page
        .getByLabel("Sign-in name", { exact: true })
        .fill("warehouse-reviewer-fixed");
      await expect(
        dialog.getByRole("button", { name: "Continue", exact: true }),
      ).toBeEnabled();
    } else {
      await expect(
        dialog.getByRole("button", { name: "Create account", exact: true }),
      ).toBeDisabled();
      await expect(dialog.getByRole("alert")).toContainText(
        "refresh recorded activity",
      );
    }
  });
}
