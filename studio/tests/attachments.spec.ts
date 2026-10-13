/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import { readFile } from "node:fs/promises";

test("downloads original conversation bytes and retains draft and selection after a failed download", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    conversation = "b".repeat(32),
    attachment = "c".repeat(32);
  const original = Buffer.from("Reserve without overselling.\n");
  let failDownload = false;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith(`/${attachment}/content`)) {
      expect(route.request().method()).toBe("GET");
      return failDownload
        ? route.fulfill({
            status: 409,
            json: { detail: "Content unavailable" },
          })
        : route.fulfill({ contentType: "text/markdown", body: original });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: repository,
            name: "Inventory",
            provider: "git",
            url: "file:///inventory",
            default_branch: "main",
          },
        ]
      : path.endsWith("/product") || path.endsWith("/brand")
        ? null
        : path.endsWith("/assistant/conversations")
          ? [
              {
                id: conversation,
                task: "product",
                agent: "bridge",
                created_at: "2026-10-12T12:00:00Z",
              },
            ]
          : path.endsWith(`/assistant/conversations/${conversation}`)
            ? {
                id: conversation,
                task: "product",
                agent: "bridge",
                turns: [],
                journal: { verified: true },
              }
            : path.endsWith("/attachments")
              ? [
                  {
                    id: attachment,
                    request_id: "d".repeat(32),
                    filename: "requirements.md",
                    mime_type: "text/markdown",
                    kind: "document",
                    content_bytes: original.length,
                    sha256: "e".repeat(64),
                    status: "ready",
                    text_chars: original.length,
                    truncated: false,
                    created_at: "2026-10-12T12:00:00Z",
                  },
                ]
              : [];
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
    .getByLabel("Product design message", { exact: true })
    .fill("Keep this unsent design draft.");
  await page.getByRole("button", { name: "Files", exact: true }).click();
  const selected = page.getByLabel("Include requirements.md in next message", {
    exact: true,
  });
  await selected.check();
  const downloadEvent = page.waitForEvent("download");
  await page
    .getByRole("button", { name: "Download requirements.md", exact: true })
    .click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("requirements.md");
  expect(await readFile((await download.path())!)).toEqual(original);
  await expect(selected).toBeChecked();
  failDownload = true;
  await page
    .getByRole("button", { name: "Download requirements.md", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("409");
  await expect(selected).toBeChecked();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toHaveValue("Keep this unsent design draft.");
  await expect(
    page.locator('[aria-label="Files for next message"]'),
  ).toContainText("requirements.md");
});

for (const unknownReply of [false, true]) {
  test(`conversation documents use saved IDs and ${unknownReply ? "resolve a lost upload reply without replay" : "retain file history after sending"}`, async ({
    page,
  }) => {
    const repository = "a".repeat(32),
      conversation = "b".repeat(32),
      id = "c".repeat(32);
    let files: Record<string, unknown>[] = [];
    const uploads: Record<string, unknown>[] = [],
      turns: Record<string, unknown>[] = [];
    let sent = false;
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
            task: "product",
            agent: "bridge",
            created_at: "2026-10-12T12:00:00Z",
          },
        ];
      if (path.endsWith(`/assistant/conversations/${conversation}`))
        json = {
          id: conversation,
          repository_id: repository,
          task: "product",
          agent: "bridge",
          created_at: "2026-10-12T12:00:00Z",
          journal: { verified: true },
          turns: sent
            ? [
                {
                  id: "d".repeat(32),
                  status: "completed",
                  message: "Use these requirements",
                  created_at: "2026-10-12T12:00:00Z",
                  context: { attachments: files },
                  reply: {
                    message: "The supplied document is recorded.",
                    questions: [],
                    assumptions: [],
                    proposal: null,
                    citation_ids: [],
                  },
                },
              ]
            : [],
        };
      if (path.endsWith("/attachments")) {
        if (req.method() === "POST") {
          const body = req.postDataJSON();
          uploads.push(body);
          files = [
            {
              id,
              request_id: body.request_id,
              filename: body.filename,
              mime_type: "text/markdown",
              kind: "document",
              content_bytes: 25,
              sha256: "e".repeat(64),
              status: "ready",
              text_chars: 25,
              truncated: false,
              created_at: "2026-10-12T12:00:00Z",
            },
          ];
          if (unknownReply) return route.abort("failed");
          json = files[0];
        } else json = files;
      }
      if (path.endsWith("/turns")) {
        turns.push(req.postDataJSON());
        sent = true;
        json = { id: "d".repeat(32), status: "completed" };
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
    await page.getByRole("button", { name: "Files", exact: true }).click();
    const dialog = page.getByRole("dialog", {
      name: "Conversation files",
      exact: true,
    });
    await expect(
      page.getByRole("button", { name: "Choose a file", exact: true }),
    ).toBeEnabled();
    await page
      .getByLabel("Choose conversation file", { exact: true })
      .setInputFiles({
        name: "requirements.md",
        mimeType: "text/markdown",
        buffer: Buffer.from("Reserve without overselling"),
      });
    await page
      .getByLabel("I checked this file for credentials or secrets.", {
        exact: true,
      })
      .check();
    await page
      .getByRole("button", { name: "Upload and attach", exact: true })
      .click();
    if (unknownReply) {
      await expect(dialog.getByRole("alert")).toContainText(
        "Check recorded files",
      );
      expect(uploads).toHaveLength(1);
      await page
        .getByRole("button", { name: "Check upload", exact: true })
        .click();
    }
    await expect(
      dialog
        .locator(".conversation-saved-files")
        .getByText("requirements.md", { exact: true }),
    ).toBeVisible();
    expect(uploads).toHaveLength(1);
    expect(uploads[0].request_id).toMatch(/^[a-f0-9]{32}$/);
    expect(uploads[0].content_base64).toBe(
      Buffer.from("Reserve without overselling").toString("base64"),
    );
    await page.getByRole("button", { name: "Done", exact: true }).click();
    await expect(
      page.getByRole("region", { name: "Files for next message" }),
    ).toHaveCount(0);
    await expect(
      page.locator('[aria-label="Files for next message"]'),
    ).toContainText("requirements.md");
    await page
      .getByLabel("Product design message", { exact: true })
      .fill("Use these requirements");
    await page
      .getByRole("button", { name: "Send design message", exact: true })
      .click();
    expect(turns).toHaveLength(1);
    expect(turns[0].attachment_ids).toEqual([id]);
    expect(turns[0]).not.toHaveProperty("content_base64");
    await expect(
      page.locator('[aria-label="Recorded message files"]'),
    ).toContainText("requirements.md");
    expect(uploads).toHaveLength(1);
  });
}

test("unsupported image input rejection preserves message and permits detaching the image", async ({
  page,
}) => {
  const repository = "a".repeat(32),
    conversation = "b".repeat(32),
    image = {
      id: "c".repeat(32),
      request_id: "d".repeat(32),
      filename: "screen.png",
      mime_type: "image/png",
      kind: "image",
      content_bytes: 100,
      sha256: "e".repeat(64),
      status: "ready",
      text_chars: 0,
      truncated: false,
      created_at: "2026-10-12T12:00:00Z",
    };
  let attempts = 0;
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
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
        created_at: "2026-10-12T12:00:00Z",
        journal: { verified: true },
        turns: [],
      };
    if (path.endsWith("/attachments")) json = [image];
    if (path.endsWith("/turns")) {
      attempts++;
      return route.fulfill({
        status: 409,
        json: { detail: "Image input requires a verified model" },
      });
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
  await page.getByRole("button", { name: "Files", exact: true }).click();
  await page
    .getByLabel("Include screen.png in next message", { exact: true })
    .check();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  for (const [width, height] of [
    [1440, 768],
    [390, 844],
    [320, 568],
  ]) {
    await page.setViewportSize({ width, height });
    if (width < 1100)
      await page
        .getByRole("button", { name: "Open design assistant", exact: true })
        .click();
    const panel = await page
      .getByRole(width < 1100 ? "dialog" : "complementary", {
        name: "Product design assistant",
        exact: true,
      })
      .boundingBox();
    const send = await page
      .getByRole("button", { name: "Send design message", exact: true })
      .boundingBox();
    await page.screenshot({
      path: test.info().outputPath(`attachments-${width}.png`),
    });
    expect(send!.y + send!.height).toBeLessThanOrEqual(
      panel!.y + panel!.height - 8,
    );
    expect(send!.width).toBeGreaterThan(100);
    if (width < 1100)
      await page
        .getByRole("button", { name: "Close design assistant", exact: true })
        .click();
  }
  await page.setViewportSize({ width: 1440, height: 768 });
  await page
    .getByLabel("Product design message", { exact: true })
    .fill("Inspect this screen");
  await page
    .getByRole("button", { name: "Send design message", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Image input requires a verified model",
  );
  await expect(
    page.getByLabel("Product design message", { exact: true }),
  ).toHaveValue("Inspect this screen");
  await expect(
    page.getByText("The last request outcome is uncertain.", { exact: false }),
  ).toHaveCount(0);
  await page
    .getByRole("button", {
      name: "Remove screen.png from message",
      exact: true,
    })
    .click();
  expect(attempts).toBe(1);
});
