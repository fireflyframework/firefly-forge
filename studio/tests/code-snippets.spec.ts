/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { assistantChatFixture } from "./assistant-chat-fixture";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";

for (const width of [1440, 320])
  for (const theme of ["light", "dark"]) {
    test(`Markdown snippets preserve code and highlight real grammar at ${width}px ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize({ width, height: width === 320 ? 568 : 900 });
      await page.addInitScript(() =>
        Object.defineProperty(navigator, "clipboard", {
          configurable: true,
          value: {
            writeText: async (value: string) => {
              (window as Window & { snippetCopy?: string }).snippetCopy = value;
            },
          },
        }),
      );
      const repository = "a".repeat(32),
        fixture = assistantChatFixture(repository);
      const source =
        '# Reservation note: España 😀\ndef reserve(quantity):\n    return "ready" + "' +
        "long-code-".repeat(35) +
        '"\n';
      const unknown =
        '<img src=x onerror="alert(1)">\n<script>alert("x")</script>\n';
      (fixture.state.turns[0].reply as Record<string, unknown>).message =
        "Use `quantity` as input.\n\n```py\n" +
        source +
        "\n```\n\n```unknown-language\n" +
        unknown +
        "\n```";
      await page.route("**/api/v1/**", async (route) => {
        if ((await mockIdentity(route)) || (await fixture.handle(route)))
          return;
        const path = new URL(route.request().url()).pathname;
        await route.fulfill({
          json: path.endsWith("/repositories")
            ? [
                {
                  id: repository,
                  name: "Snippet fixture",
                  provider: "git",
                  url: "/local/snippet-fixture",
                  default_branch: "main",
                },
              ]
            : path.endsWith("/product") || path.endsWith("/brand")
              ? null
              : [],
        });
      });
      await page.goto("/");
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await navigate(page, "Connections");
      await page
        .getByLabel("API token", { exact: true })
        .fill("fixture-snippets-operator-at-least-24-characters");
      await page
        .getByRole("button", { name: "Save connection", exact: true })
        .click();
      await navigate(page, "Product definition");
      if (width === 320)
        await page
          .getByRole("button", { name: "Open design assistant", exact: true })
          .click();
      const snippet = page
        .locator(".assistant-ai-message .code-snippet")
        .first();
      await expect(snippet).toHaveAttribute("data-highlight", "grammar");
      await snippet.scrollIntoViewIfNeeded();
      await expect(snippet.locator(".hljs-keyword").first()).toBeVisible();
      await expect(snippet.locator(".hljs-string").first()).toBeVisible();
      expect(await snippet.locator("code").textContent()).toBe(source);
      const beforeCopy = (await snippet.boundingBox())!.height;
      await snippet
        .getByRole("button", { name: "Copy code", exact: true })
        .click();
      await expect
        .poll(() =>
          page.evaluate(
            () => (window as Window & { snippetCopy?: string }).snippetCopy,
          ),
        )
        .toBe(source);
      expect((await snippet.boundingBox())!.height).toBe(beforeCopy);
      const geometry = await snippet.evaluate((figure) => {
        const pre = figure.querySelector("pre")!;
        function rgb(value: string) {
          return (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
        }
        function light(value: number[]) {
          const c = value.map((n) => {
            const v = n / 255;
            return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
          });
          return c[0] * 0.2126 + c[1] * 0.7152 + c[2] * 0.0722;
        }
        const background = light(rgb(getComputedStyle(figure).backgroundColor));
        const contrasts = [
          ...figure.querySelectorAll(
            ".hljs-keyword,.hljs-string,.hljs-comment",
          ),
        ].map((node) => {
          const color = light(rgb(getComputedStyle(node).color));
          return (
            (Math.max(color, background) + 0.05) /
            (Math.min(color, background) + 0.05)
          );
        });
        return {
          contrasts,
          scroll: pre.scrollWidth > pre.clientWidth,
          wrap: getComputedStyle(pre).whiteSpace,
          pageFits: document.documentElement.scrollWidth <= innerWidth + 1,
        };
      });
      expect(geometry.scroll).toBe(true);
      expect(geometry.wrap).toBe("pre");
      expect(geometry.pageFits).toBe(true);
      expect(Math.min(...geometry.contrasts)).toBeGreaterThanOrEqual(4.5);
      await snippet.screenshot({
        path: info.outputPath(`snippet-${width}-${theme}.png`),
      });
      const plain = page.locator(".assistant-ai-message .code-snippet").nth(1);
      await expect(plain).toHaveAttribute("data-highlight", "plain");
      expect(await plain.locator("code").textContent()).toBe(unknown);
      await expect(plain.locator("script,img,[onerror],[onload]")).toHaveCount(
        0,
      );
      expect(fixture.state.created).toBe(0);
      expect(fixture.state.sent).toBe(0);
    });
  }
