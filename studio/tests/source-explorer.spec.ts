/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";

const hash = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const longPath =
  "backend/inventory/reconciliation/domain/operations/retained_reservations.ts";
const opening = `// Controlled archived source fixture\nexport type Reservation = { sku: string; quantity: number };\nexport function retainStock(input: Reservation) {\n  const html = "<img src=x onerror=window.__SOURCE_EXECUTED__=true>";\n  // \`\`\` This is source text, never a Markdown fence.\n  return { ...input, retained: true, html };\n}\n`;
let prefix = Buffer.from(opening);
const comment = Buffer.from(
  "// Controlled pagination marker for recorded source.\n",
);
while (prefix.length + comment.length + 3 < 32767)
  prefix = Buffer.concat([prefix, comment]);
prefix = Buffer.concat([
  prefix,
  Buffer.from("// " + " ".repeat(32767 - prefix.length - 3)),
]);
const source = Buffer.concat([
  prefix,
  Buffer.from("€\nexport const retainedAfterBoundary = true;\n".repeat(900)),
]);
const binary = Buffer.from([255, 0, 128, 1]);
const frontend = Buffer.from('export const columns = ["sku", "available"];\n');
function reference(runId = "a".repeat(32), second = false) {
  return {
    protocol: "forge-source-archive-v1",
    snapshot_id: (second ? "c" : "b").repeat(32),
    tenant_id: "1".repeat(32),
    project_id: "2".repeat(32),
    repository_id: "3".repeat(32),
    run_id: runId,
    attempt: 3,
    head: "4".repeat(40),
    tree: "5".repeat(40),
    branch: second ? "release/retained-ui" : "forge/source-review",
    storage_binding: "9".repeat(64),
    catalog: {
      version: 1,
      artifact_id: (second ? "6" : "7").repeat(32),
      manifest_sha256: (second ? "6" : "8").repeat(64),
      source_digest: "e".repeat(64),
      manifest_digest: "f".repeat(64),
      byte_count: source.length + binary.length + frontend.length,
      file_count: 3,
      entry_count: 5,
      page_count: 2,
    },
  };
}
const file = (path: string, data: Buffer) => ({
  path,
  kind: "file",
  mode: 33188,
  byte_count: data.length,
  sha256: hash(data),
});
const directory = (path: string) => ({
  path,
  kind: "directory",
  mode: 16877,
  byte_count: 0,
  sha256: null,
});
type Control = {
  mode: string;
  calls: string[];
  writes: number;
  external: number;
  delay: boolean;
};
async function intercept(page: Page): Promise<Control> {
  const control: Control = {
    mode: "published",
    calls: [],
    writes: 0,
    external: 0,
    delay: false,
  };
  await page.route("**/*", async (route) => {
    const req = route.request(),
      url = new URL(req.url());
    if (url.hostname !== "127.0.0.1" || req.method() !== "GET") {
      if (req.method() !== "GET") control.writes++;
      else control.external++;
      return route.abort();
    }
    if (!url.pathname.startsWith("/api/v1/")) return route.continue();
    control.calls.push(url.pathname + url.search);
    if (control.mode === "revoked")
      return route.fulfill({
        status: 403,
        json: { detail: "Controlled current scope revoked" },
      });
    const runId = url.pathname.split("/")[4];
    const second = url.pathname.includes("/" + "c".repeat(32) + "/");
    const ref = reference(runId, second);
    if (url.pathname.endsWith("/source/revisions")) {
      const items =
        control.mode === "empty"
          ? []
          : [
              {
                reference: reference(runId),
                status: ["pending", "failed"].includes(control.mode)
                  ? control.mode
                  : "published",
                reason:
                  control.mode === "failed"
                    ? "source_publication_failed_reconciliation_required"
                    : null,
              },
              ...(["pending", "failed"].includes(control.mode)
                ? []
                : [
                    {
                      reference: reference(runId, true),
                      status: "published",
                      reason: null,
                    },
                  ]),
            ];
      return route.fulfill({
        json: {
          run_id: runId,
          items,
          next_after: null,
          recording_enabled: true,
          availability: items.length ? "recorded" : "unavailable",
        },
      });
    }
    expect(url.searchParams.get("expected_manifest")).toBe(
      ref.catalog.manifest_sha256,
    );
    const n = Number(url.searchParams.get("page"));
    const entries =
      n === 0
        ? [
            directory("backend/"),
            file(longPath, source),
            file("backend/review/retained-byte-evidence.bin", binary),
          ]
        : [
            directory("frontend/"),
            file("frontend/views/StockLedger.tsx", frontend),
          ];
    if (url.pathname.endsWith("/catalog"))
      return route.fulfill({
        json: {
          reference: ref,
          catalog: {
            version: 1,
            artifact_id: ref.catalog.artifact_id,
            manifest_sha256: ref.catalog.manifest_sha256,
            page: n,
            next_page: n === 0 ? 1 : null,
            entries,
          },
        },
      });
    const index = Number(url.pathname.split("/").at(-1)),
      offset = Number(url.searchParams.get("offset")),
      limit = Number(url.searchParams.get("limit"));
    expect(limit === 32768 || (limit >= 1 && limit <= 3)).toBe(true);
    if (control.delay && runId === "a".repeat(32))
      await new Promise((resolve) => setTimeout(resolve, 500));
    const data = n === 1 ? frontend : index === 2 ? binary : source;
    const content = data.subarray(offset, offset + limit);
    let text: string | null;
    try {
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
        content,
      );
      if (text.includes("\0")) text = null;
    } catch {
      text = null;
    }
    const json = {
      reference: ref,
      entry: entries[index],
      catalog_page: n,
      entry_index: index,
      offset,
      byte_count: content.length,
      next_offset:
        offset + content.length < data.length ? offset + content.length : null,
      sha256: control.mode === "corrupt" ? "0".repeat(64) : hash(content),
      content_base64: content.toString("base64"),
      binary: text === null,
      text,
    };
    try {
      await route.fulfill({ json });
    } catch {
      /* A scoped view change can abort this controlled response. */
    }
  });
  return control;
}
for (const [width, theme] of [
  [1440, "light"],
  [1440, "dark"],
  [390, "light"],
  [390, "dark"],
] as const) {
  test(`controlled source explorer ${width} ${theme}`, async ({
    page,
  }, info) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const control = await intercept(page);
    await page.goto(`/__source_explorer__?theme=${theme}`);
    const panel = page.getByRole("region", {
      name: "Recorded source snapshot",
      exact: true,
    });
    await expect(
      panel.getByText("Window checksum verified", { exact: true }),
    ).toBeVisible();
    await expect(
      panel.getByText(/Text preview aligned to UTF-8/),
    ).toBeVisible();
    await expect(panel.getByLabel("Recorded source code")).toContainText(
      "retainStock",
    );
    await expect(panel.getByLabel("Recorded source code")).toContainText("€");
    expect(await page.locator("img").count()).toBe(0);
    expect(
      await page.evaluate(() => Reflect.get(window, "__SOURCE_EXECUTED__")),
    ).toBeUndefined();
    await page.evaluate(() => document.fonts.ready);
    const geometry = await panel.evaluate((element) => ({
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
      font: getComputedStyle(element).fontFamily,
      targets: [...element.querySelectorAll("button,select,summary")]
        .filter((x) => x.getClientRects().length > 0)
        .map((x) => x.getBoundingClientRect().height),
      footer:
        document
          .querySelector(".source-fixture-footer")!
          .getBoundingClientRect().top >=
        element.getBoundingClientRect().bottom,
    }));
    expect(geometry.overflow).toBe(false);
    expect(geometry.font).toContain("Manrope");
    expect(geometry.targets.every((n) => n >= 44)).toBe(true);
    expect(geometry.footer).toBe(true);
    await page.screenshot({
      path: info.outputPath(`source-${width}-${theme}.png`),
      fullPage: false,
    });
    await panel
      .getByRole("button", { name: "Next window", exact: true })
      .click();
    await expect(panel).toContainText("Bytes 32,769");
    await expect(panel.getByLabel("Recorded source code")).not.toContainText(
      "�",
    );
    if (width === 390)
      await panel
        .getByRole("button", { name: "Browse files", exact: true })
        .click();
    await panel
      .getByRole("button", { name: /backend\/review\/retained-byte-evidence/ })
      .focus();
    await page.keyboard.press("Enter");
    await expect(panel.getByLabel("Exact source bytes")).toHaveText(
      "ff 00 80 01",
    );
    await expect(
      panel.getByRole("region", { name: "Recorded file content", exact: true }),
    ).toBeFocused();
    if (width === 390)
      await panel
        .getByRole("button", { name: "Browse files", exact: true })
        .click();
    await panel
      .getByRole("button", { name: "Next files", exact: true })
      .click();
    await expect(panel.getByLabel("Recorded source code")).toContainText(
      "columns",
    );
    await panel
      .getByRole("combobox", { name: "Recorded branch", exact: true })
      .selectOption("release/retained-ui");
    await expect(
      panel.getByText("Window checksum verified", { exact: true }),
    ).toBeVisible();
    control.mode = "corrupt";
    await panel
      .getByRole("button", { name: "Next window", exact: true })
      .click();
    await expect(
      panel.getByText("Snapshot needs review", { exact: true }),
    ).toBeVisible();
    await expect(panel.getByLabel("Recorded source code")).toHaveCount(0);
    control.mode = "pending";
    await panel
      .getByRole("button", { name: "Refresh records", exact: true })
      .click();
    await expect(
      panel.getByText("Publication is incomplete", { exact: true }),
    ).toBeVisible();
    control.mode = "empty";
    await panel
      .getByRole("button", { name: "Refresh records", exact: true })
      .click();
    await expect(
      panel.getByText("No recorded source", { exact: true }),
    ).toBeVisible();
    control.mode = "published";
    await panel
      .getByRole("button", { name: "Refresh records", exact: true })
      .click();
    await expect(
      panel.getByText("Window checksum verified", { exact: true }),
    ).toBeVisible();
    control.mode = "revoked";
    await panel
      .getByRole("button", { name: "Next window", exact: true })
      .click();
    await expect(
      panel.getByText("Source access is unavailable", { exact: true }),
    ).toBeVisible();
    await expect(panel.getByLabel("Recorded source code")).toHaveCount(0);
    expect(control.writes).toBe(0);
    expect(control.external).toBe(0);
    expect(errors).toEqual([]);
  });
}
test("a late response from the previous run cannot cross a scoped view switch", async ({
  page,
}) => {
  const control = await intercept(page);
  control.delay = true;
  await page.goto("/__source_explorer__?theme=light");
  await expect(
    page.getByText("Verifying source window…", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    window.dispatchEvent(
      new CustomEvent("source-fixture", {
        detail: { runId: "f".repeat(32), token: "other-scoped-fixture-token" },
      }),
    ),
  );
  await expect(
    page.getByText("Window checksum verified", { exact: true }),
  ).toBeVisible();
  await page.getByText("View snapshot provenance", { exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Recorded source snapshot" }),
  ).not.toContainText("a".repeat(32));
  expect(control.calls.some((url) => url.includes("f".repeat(32)))).toBe(true);
  expect(control.writes).toBe(0);
});
