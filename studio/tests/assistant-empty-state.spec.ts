/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

for (const width of [1440, 390]) {
  for (const outcome of ["completed", "uncertain"] as const) {
    test(`empty assistant starts one conversation before explicit ${outcome} message at ${width}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 900 });
      const repository = "a".repeat(32),
        conversation = "b".repeat(32);
      let created = 0,
        sent = 0;
      let releaseCreation: () => void = () => {};
      const creation = new Promise<void>((resolve) => {
        releaseCreation = resolve;
      });
      let sessions: Record<string, unknown>[] = [];
      let turns: Record<string, unknown>[] = [];
      const record = {
        id: conversation,
        repository_id: repository,
        agent: "agentic",
        task: "functions",
        created_at: "2026-10-22T00:00:00Z",
      };
      await page.route("**/api/v1/**", async (route) => {
        if (await mockIdentity(route)) return;
        const request = route.request(),
          path = new URL(request.url()).pathname;
        let json: unknown = [];
        if (path.endsWith("/repositories"))
          json = [
            {
              id: repository,
              name: "Fictional inventory",
              provider: "git",
              url: "/local/fictional-inventory",
              default_branch: "main",
            },
          ];
        if (path.endsWith("/product") || path.endsWith("/brand")) json = null;
        if (path.endsWith("/assistant/conversations")) {
          if (request.method() === "POST") {
            created++;
            expect(request.postDataJSON()).toEqual({ task: "functions" });
            await creation;
            sessions = [record];
            json = record;
          } else json = sessions;
        }
        if (path.endsWith(`/assistant/conversations/${conversation}`))
          json = {
            ...record,
            turns,
            journal: { verified: true, head: "c".repeat(64), entries: [] },
          };
        if (path.endsWith("/capabilities"))
          json = { image_input: { status: "unverified" } };
        if (path.endsWith("/turns")) {
          sent++;
          const body = request.postDataJSON();
          expect(body.message).toBe("Describe the reservation journey");
          expect(body.request_id).toMatch(/^[a-f0-9]{32}$/);
          if (outcome === "uncertain") return route.abort("failed");
          turns = [
            {
              id: "d".repeat(32),
              request_id: body.request_id,
              message: body.message,
              status: "completed",
              created_at: "2026-10-22T00:00:00Z",
              reply: {
                message: "Review the recorded reservation journey.",
                questions: [],
                assumptions: [],
                citation_ids: [],
                proposal: null,
              },
            },
          ];
          json = turns[0];
        }
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
      if (width === 390)
        await page
          .getByRole("button", { name: "Open design assistant", exact: true })
          .click();
      const message = page.getByLabel("Product design message", {
        exact: true,
      });
      await expect(message).toBeVisible();
      await expect(message).toBeEnabled();
      await page
        .getByLabel("Task for next conversation", { exact: true })
        .selectOption("functions");
      expect(created).toBe(0);
      expect(sent).toBe(0);
      await message.fill("Describe the reservation journey");
      const send = page.locator(".assistant-send");
      await send.click();
      await expect(send).toBeDisabled();
      await expect(
        page.getByRole("button", { name: "New conversation", exact: true }),
      ).toBeDisabled();
      await page
        .locator(".assistant-composer")
        .evaluate((form: HTMLFormElement) => form.requestSubmit());
      expect(created).toBe(1);
      expect(sent).toBe(0);
      releaseCreation();
      await expect.poll(() => sent).toBe(1);
      if (outcome === "completed")
        await expect(
          page.getByText("Review the recorded reservation journey.", {
            exact: true,
          }),
        ).toBeVisible();
      else {
        await expect(page.locator(".integration-status")).toContainText(
          "last request outcome is uncertain",
        );
        await expect(message).toBeDisabled();
        await expect(send).toBeDisabled();
        await page
          .getByRole("button", { name: "Conversation history", exact: true })
          .click();
        await page
          .getByRole("button", { name: "Refresh record", exact: true })
          .click();
        await expect(send).toBeDisabled();
      }
      expect(created).toBe(1);
      expect(sent).toBe(1);
    });
  }
}
