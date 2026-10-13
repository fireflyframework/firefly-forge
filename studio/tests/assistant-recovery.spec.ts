/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
for (const mode of ["expired", "legacy", "lost"] as const) {
  test(`interrupted ${mode} response requires explicit recovery and never repeats a model request`, async ({
    page,
  }) => {
    const repository = "a".repeat(32),
      conversation = "b".repeat(32),
      turn = "c".repeat(32);
    const admissions: Record<string, unknown>[] = [];
    let recovered = false,
      modelCalls = 0;
    await page.route("**/api/v1/**", async (route) => {
      if (await mockIdentity(route)) return;
      const req = route.request(),
        path = new URL(req.url()).pathname;
      let json: unknown = [];
      if (path.endsWith("/repositories"))
        json = [
          {
            id: repository,
            name: "Inventory",
            url: "https://github.com/example/inventory",
            provider: "github",
            default_branch: "main",
          },
        ];
      if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
      if (path.endsWith("/assistant/conversations"))
        json = [
          {
            id: conversation,
            repository_id: repository,
            agent: "bridge",
            created_at: "2026-10-12T12:00:00Z",
          },
        ];
      if (path.endsWith(`/assistant/conversations/${conversation}`))
        json = {
          id: conversation,
          repository_id: repository,
          agent: "bridge",
          journal: { verified: true },
          turns: [
            {
              id: turn,
              request_id: "d".repeat(32),
              message: "Review reservation behavior",
              status: recovered ? "needs_reconciliation" : "processing",
              created_at: "2026-10-12T12:00:00Z",
              reply: null,
              ownership: {
                owner_id: mode === "legacy" ? null : "worker-1",
                claim_epoch: mode === "legacy" ? 0 : 4,
                lease_expires_at: "2026-10-12T11:00:00Z",
                last_heartbeat_at: "2026-10-12T10:59:00Z",
              },
              recovery: {
                eligible: !recovered && mode !== "legacy",
                expected_epoch: recovered ? 5 : mode === "legacy" ? 0 : 4,
                legacy_confirmation_required: !recovered && mode === "legacy",
              },
            },
          ],
        };
      if (path.endsWith("/capabilities"))
        json = { image_input: { status: "unverified" } };
      if (path.endsWith("/recover")) {
        admissions.push(req.postDataJSON());
        recovered = true;
        if (mode === "lost") return route.abort("failed");
        json = { id: turn, status: "needs_reconciliation" };
      }
      if (path.endsWith("/turns")) modelCalls++;
      await route.fulfill({ json });
    });
    await page.goto("/");
    await navigate(page, "Connections");
    await page
      .getByLabel("API token", { exact: true })
      .fill("test-bootstrap-at-least-24-characters");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await navigate(page, "Product definition");
    await page
      .getByRole("button", { name: "Review interrupted response", exact: true })
      .click();
    const modal = page.getByRole("dialog", {
      name: "Review interrupted response",
      exact: true,
    });
    await expect(modal).toBeVisible();
    expect(admissions).toHaveLength(0);
    const confirm = modal.getByRole("button", {
      name: "Record interrupted outcome",
      exact: true,
    });
    if (mode === "legacy") {
      await expect(confirm).toBeDisabled();
      await expect(
        page.getByLabel("I confirmed that the original worker stopped.", {
          exact: true,
        }),
      ).not.toBeChecked();
      await page
        .getByLabel("I confirmed that the original worker stopped.", {
          exact: true,
        })
        .check();
    }
    await confirm.click();
    if (mode === "lost") {
      await expect(modal.getByRole("alert")).toContainText(
        "result is not confirmed",
      );
      await expect(
        modal.getByRole("button", {
          name: "Record interrupted outcome",
          exact: true,
        }),
      ).toHaveCount(0);
      await modal
        .getByRole("button", { name: "Check recorded response", exact: true })
        .click();
    }
    await expect(modal).toHaveCount(0);
    await expect(page.locator(".assistant-turn")).toContainText(
      "needs reconciliation",
    );
    expect(admissions).toHaveLength(1);
    expect(admissions[0]).toMatchObject({
      expected_epoch: mode === "legacy" ? 0 : 4,
      legacy_confirmed_stopped: mode === "legacy",
    });
    expect(admissions[0].request_id).toMatch(/^[a-f0-9]{32}$/);
    expect(modelCalls).toBe(0);
  });
}
