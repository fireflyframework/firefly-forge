/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { mockFactoryDashboard } from "./factory-dashboard-fixture";
import {
  expect,
  test,
  type Locator,
  type Page,
  type Route,
} from "@playwright/test";
import { navigate } from "./navigation";
import { captureWalkthrough } from "./walkthrough-capture";
import {
  corpus,
  counts,
  completeCounts,
  completeSources,
  generationCandidate,
  discovery,
} from "./framework-knowledge-fixture";
import type { FrameworkOperation } from "../src/FrameworkKnowledgeData";

const oldCorpus = {
  ...corpus,
  corpus_ref: "7".repeat(32),
  framework_version: "26.10.6",
  release_tag: "v26.10.6",
  commit: "8".repeat(40),
  workspace_id: "framework-pyfly-26-10-6",
};
const status = {
  ...discovery.status,
  current_corpus: oldCorpus,
  latest_stable: discovery.release,
  checked_at: discovery.release.checked_at,
  counts: {
    ...counts,
    files: 7,
    total_files: 7,
    documents: 7,
    parts: 9,
    ready_parts: 9,
    bytes: 2816,
  },
  state: "update_available",
  allowed_actions: ["check_updates", "refresh", "browse", "search"],
};
const larafly = {
  ...status,
  framework: "larafly",
  official_repository: "fireflyframework/fireflyframework-php",
  current_corpus: {
    ...oldCorpus,
    framework: "larafly",
    framework_version: "26.10.1",
    release_tag: "v26.10.1",
    corpus_ref: "6".repeat(32),
    commit: "5".repeat(40),
    workspace_id: "framework-larafly-26-10-1",
  },
  latest_stable: {
    ...discovery.release,
    framework: "larafly",
    framework_version: "26.10.4",
    release_tag: "v26.10.4",
    repository: "fireflyframework/fireflyframework-php",
    release_url:
      "https://github.com/fireflyframework/fireflyframework-php/releases/tag/v26.10.4",
  },
};
function operation(id: string): FrameworkOperation {
  return {
    request_id: id,
    framework: "pyfly",
    corpus,
    state: "needs_reconciliation",
    stage: "submitting",
    counts: { ...counts, files: 45, total_files: 202, documents: 31 },
    spent_budget: { requests: 5, bytes: 1234, seconds: 2 },
    remaining_budget: { requests: 20, bytes: 8000, seconds: 60 },
    sanitized_error: null,
    uncertain_sources: 1,
    allowed_actions: ["observe"],
    revision: "9".repeat(64),
  };
}
async function factory(
  page: Page,
  custom?: (route: Route) => Promise<boolean>,
) {
  await page.route("**/api/v1/**", async (route) => {
    if (await mockFactoryDashboard(route)) return;
    if (custom && (await custom(route))) return;
    const path = new URL(route.request().url()).pathname;
    let json: unknown = [];
    if (path.endsWith("/auth/configuration"))
      json = { mode: "local_bootstrap", issuer: "", cookie_secure: false };
    else if (path.endsWith("/auth/session"))
      json = {
        authenticated: true,
        user: {
          id: "f".repeat(32),
          display_name: "Knowledge operator",
          email: null,
          authentication_method: "local_bootstrap",
        },
        global_admin: true,
        memberships: [],
        roles: {},
      };
    else if (path.endsWith("/health")) json = { execution_ready: true };
    else if (path.endsWith("/profiles")) json = {};
    else if (path.endsWith("/knowledge/configuration"))
      json = {
        enabled: true,
        required: true,
        framework_workspace: "frameworks",
        readiness: { state: "ready", detail: "Controlled UI fixture" },
      };
    else if (path.endsWith("/knowledge/frameworks/status"))
      json = { version: 1, frameworks: [status, larafly] };
    else if (path.endsWith("/corpora"))
      json = { items: [oldCorpus, corpus], next_cursor: null, total: 2 };
    else if (path.endsWith("/sources"))
      json = path.includes(`/corpora/${corpus.corpus_ref}/`)
        ? {
            items: completeSources,
            next_cursor: null,
            total: completeSources.length,
          }
        : { items: [], next_cursor: null, total: 0 };
    else if (path.endsWith("/check-updates")) json = { ...discovery, status };
    await route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Framework releases", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "pyfly", exact: true }),
  ).toBeVisible();
}

async function focusedFrameworkCapture(
  page: Page,
  section: Locator,
  filename: string,
  canonical?: {
    stage: string;
    theme: string;
    device: string;
    selector: string;
    actionSelector: string;
  },
) {
  const viewport = page.viewportSize();
  if (!viewport)
    throw new Error("A viewport is required for controlled captures.");
  const height = await section.evaluate(
    (element) => element.getBoundingClientRect().height,
  );
  await page.setViewportSize({
    ...viewport,
    height: Math.max(viewport.height, Math.ceil(height) + 280),
  });
  await section.evaluate((element) =>
    element.scrollIntoView({ block: "start" }),
  );
  const main = await page.locator(".workspace-main > main").boundingBox();
  const bounds = await section.boundingBox();
  if (!main || !bounds)
    throw new Error("The complete section must be visible before capture.");
  expect(bounds.y).toBeGreaterThanOrEqual(main.y);
  expect(bounds.y + bounds.height).toBeLessThanOrEqual(main.y + main.height);
  await section.screenshot({ path: test.info().outputPath(filename) });
  if (canonical)
    await captureWalkthrough(
      page,
      canonical.stage,
      canonical.theme,
      canonical.device,
      {
        producer: "studio/tests/framework-knowledge.spec.ts",
        boundary: "section",
        sectionSelector: canonical.selector,
        actionSelector: canonical.actionSelector,
      },
    );
  await page.setViewportSize(viewport);
}

async function frameworkContextCapture(page: Page, filename: string) {
  const viewport = page.viewportSize();
  if (!viewport)
    throw new Error("A viewport is required for controlled captures.");
  const main = page.locator(".workspace-main > main");
  const height = await main.evaluate((element) => element.scrollHeight);
  await page.setViewportSize({
    ...viewport,
    height: Math.max(viewport.height, Math.ceil(height) + 280),
  });
  await main.evaluate((element) => element.scrollTo({ top: 0 }));
  await page.screenshot({
    path: test.info().outputPath(filename),
    fullPage: true,
  });
  await page.setViewportSize(viewport);
}

test("lost refresh response retains its original ID through reload and only reads that operation", async ({
  page,
}) => {
  const submitted: string[] = [];
  const read: string[] = [];
  await factory(page, async (route) => {
    const path = new URL(route.request().url()).pathname;
    const match = path.match(/\/refreshes\/([a-f0-9]{32})$/);
    if (!match) return false;
    if (route.request().method() === "POST") {
      submitted.push(match[1]);
      await route.abort("failed");
    } else {
      read.push(match[1]);
      await route.fulfill({ json: operation(match[1]) });
    }
    return true;
  });
  await page
    .getByRole("article", { name: "pyfly", exact: true })
    .getByRole("button", { name: "Check updates", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Start refresh", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Unconfirmed refresh response" }),
  ).toBeVisible();
  expect(submitted).toHaveLength(1);
  await page.reload();
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Framework releases", exact: true })
    .click();
  await expect(page.getByText(submitted[0], { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Read original operation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: /Refresh target 26.10.11/ }),
  ).toBeVisible();
  expect(read).toEqual(submitted);
  expect(submitted).toHaveLength(1);
  await expect(
    page.getByRole("button", { name: "Start refresh", exact: true }),
  ).toHaveCount(0);
});

for (const variant of [
  { width: 1440, theme: "light" },
  { width: 320, theme: "light" },
  { width: 320, theme: "dark" },
] as const) {
  test(`a ready retained version searches without a project at ${variant.width}px ${variant.theme} and keeps item revision separate`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: variant.width, height: 1000 });
    await page.emulateMedia({ colorScheme: variant.theme });
    const searches: unknown[] = [];
    const longPath =
      "docs/application/inventory/transactional/validation/rollback/retained-version/tests/test_reservation_transaction.md";
    await factory(page, async (route) => {
      if (new URL(route.request().url()).pathname.endsWith("/sources")) {
        await route.fulfill({
          json: {
            items: [
              {
                source_ref: "1".repeat(32),
                corpus_ref: oldCorpus.corpus_ref,
                path: longPath,
                kind: "document",
                blob_oid: "3".repeat(40),
                sha256: "2".repeat(64),
                bytes: 2048,
                parts: 3,
                ready_parts: 3,
                state: "ready",
                source_uri: `https://github.com/fireflyframework/fireflyframework-pyfly/blob/${oldCorpus.commit}/${longPath}`,
                license_sha256: "4".repeat(64),
                exclusion_reason: null,
              },
            ],
            next_cursor: "9".repeat(32),
            total: 7,
          },
        });
        return true;
      }
      if (
        !new URL(route.request().url()).pathname.endsWith(
          "/frameworks/pyfly/search",
        )
      )
        return false;
      searches.push(route.request().postDataJSON());
      const hit = {
        source_ref: "1".repeat(32),
        corpus_ref: oldCorpus.corpus_ref,
        framework: "pyfly",
        framework_version: oldCorpus.framework_version,
        release_tag: oldCorpus.release_tag,
        commit: oldCorpus.commit,
        source_id: "native-source",
        chunk_id: "chunk-1",
        path: "docs/transactions.md",
        source_uri: `https://github.com/fireflyframework/fireflyframework-pyfly/blob/${oldCorpus.commit}/docs/transactions.md`,
        source_sha256: "2".repeat(64),
        license_sha256: oldCorpus.license_sha256,
        knowledge_version: 9,
        score: 0.9,
        content:
          '```python\nasync def test_transaction_rolls_back(repository):\n    await repository.reserve("SKU-104", quantity=2)\n    assert await repository.available("SKU-104") == 8\n```\n\nControlled UI example; not retrieved from a live index.',
      };
      await route.fulfill({
        json: {
          corpus: oldCorpus,
          hits: [hit],
          citations: [hit],
          warnings: [],
          context: "fixture",
        },
      });
      return true;
    });
    await page
      .getByRole("article", { name: "pyfly", exact: true })
      .getByRole("button", { name: "Browse sources", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: longPath, exact: true }),
    ).toBeVisible();
    await page.getByText("Source provenance", { exact: true }).click();
    const assertPaneFits = async () => {
      const clipped = await page
        .locator(
          ".framework-corpus-record h3, .framework-corpus-record .wizard-summary dd, .feature-record pre, .feature-record textarea, .framework-corpus-selector select",
        )
        .evaluateAll((elements) =>
          elements
            .filter((element) => {
              const rect = element.getBoundingClientRect();
              return (
                rect.width > 0 &&
                (rect.left < -1 || rect.right > window.innerWidth + 1)
              );
            })
            .map((element) => element.textContent),
        );
      expect(clipped).toEqual([]);
      const horizontalOverflow = await page
        .locator(".feature-record")
        .evaluateAll((elements) =>
          elements
            .filter((element) => element.scrollWidth > element.clientWidth + 1)
            .map((element) => element.querySelector("h2")?.textContent),
        );
      expect(horizontalOverflow).toEqual([]);
    };
    await assertPaneFits();
    await page.screenshot({
      path: test.info().outputPath("framework-sources-long-path.png"),
      fullPage: true,
    });
    await page
      .locator(".framework-corpus-record .wizard-summary > div")
      .last()
      .scrollIntoViewIfNeeded();
    await assertPaneFits();
    await page.screenshot({
      path: test.info().outputPath("framework-source-provenance-bottom.png"),
      fullPage: true,
    });
    await page.getByRole("tab", { name: "Search", exact: true }).click();
    await page
      .getByLabel("Knowledge query", { exact: true })
      .fill("transaction rollback test");
    await page
      .getByRole("button", { name: "Search knowledge", exact: true })
      .click();
    await expect(
      page.getByText("pyfly 26.10.6", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("Knowledge version 9", { exact: true }),
    ).toBeVisible();
    await page.getByText("Citation details", { exact: true }).click();
    await expect(
      page
        .locator("details")
        .filter({ has: page.getByText("Citation details", { exact: true }) })
        .getByText(oldCorpus.commit, { exact: true }),
    ).toBeVisible();
    await expect(page.locator(".knowledge-excerpt pre code")).toContainText(
      "test_transaction_rolls_back",
    );
    await assertPaneFits();
    await page.screenshot({
      path: test.info().outputPath("framework-search-code-citations.png"),
      fullPage: true,
    });
    await page
      .locator(".framework-corpus-record .wizard-summary > div")
      .last()
      .scrollIntoViewIfNeeded();
    await assertPaneFits();
    await page.screenshot({
      path: test.info().outputPath("framework-search-citation-bottom.png"),
      fullPage: true,
    });
    expect(searches).toEqual([
      { query: "transaction rollback test", corpus_ref: oldCorpus.corpus_ref },
    ]);
  });
}

test("reviewed renewal, cancellation, observation and resume retain spent work and the original operation", async ({
  page,
}) => {
  let current = {
    ...operation("4".repeat(32)),
    allowed_actions: ["renew_budget", "cancel", "observe"],
  };
  const writes: string[] = [];
  await factory(page, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/frameworks/status")) {
      await route.fulfill({
        json: {
          version: 1,
          frameworks: [
            {
              ...status,
              state:
                current.state === "queued"
                  ? "refreshing"
                  : "needs_reconciliation",
              active_operation: current,
              allowed_actions: ["check_updates", "browse", "search"],
            },
          ],
        },
      });
      return true;
    }
    const match = path.match(
      /\/refreshes\/([a-f0-9]{32})\/(budget|cancel|observe|resume)$/,
    );
    if (!match) return false;
    expect(match[1]).toBe(current.request_id);
    writes.push(match[2]);
    const spent = { ...current.spent_budget };
    if (match[2] !== "observe")
      expect(route.request().postDataJSON().expected_revision).toBe(
        current.revision,
      );
    if (match[2] === "budget") {
      expect(route.request().postDataJSON().total_budget).toEqual({
        requests: 50,
        bytes: 20000,
        seconds: 120,
      });
      current = {
        ...current,
        remaining_budget: { requests: 45, bytes: 18766, seconds: 118 },
        revision: "8".repeat(64),
      };
    } else if (match[2] === "cancel") {
      current = {
        ...current,
        state: "cancelled",
        allowed_actions: ["observe"],
        revision: "7".repeat(64),
      };
    } else if (match[2] === "observe") {
      current = {
        ...current,
        uncertain_sources: 0,
        allowed_actions: ["resume"],
        revision: "6".repeat(64),
      };
    } else {
      current = {
        ...current,
        state: "queued",
        allowed_actions: ["cancel"],
        revision: "5".repeat(64),
      };
    }
    expect(current.spent_budget).toEqual(spent);
    await route.fulfill({ json: current });
    return true;
  });
  await page.getByText("Review work allowance", { exact: true }).click();
  await page.getByLabel("Total requests", { exact: true }).fill("50");
  await page.getByLabel("Total bytes", { exact: true }).fill("20000");
  await page.getByLabel("Total seconds", { exact: true }).fill("120");
  await page
    .getByRole("button", { name: "Apply total allowance", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Cancel remaining work", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Resume reviewed import", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Observe outcome", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Resume reviewed import", exact: true })
    .click();
  await page.getByText("Operation and work budget", { exact: true }).click();
  await expect(
    page.getByText("5 requests · 1,234 bytes · 2 seconds", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: test.info().outputPath("framework-operation-retained-spend.png"),
    fullPage: true,
  });
  expect(writes).toEqual(["budget", "cancel", "observe", "resume"]);
  expect(current.request_id).toBe("4".repeat(32));
  expect(current.spent_budget).toEqual({
    requests: 5,
    bytes: 1234,
    seconds: 2,
  });
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const width of [320, 390, 1440]) {
    test(`release comparison fits ${width}px ${colorScheme} with keyboard tabs and desktop reflow`, async ({
      page,
      browser,
    }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme });
      await factory(page);
      const tab = page.getByRole("tab", {
        name: "Framework releases",
        exact: true,
      });
      await tab.focus();
      await page.keyboard.press("Home");
      await expect(
        page.getByRole("tab", { name: "Sources", exact: true }),
      ).toBeFocused();
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("ArrowRight");
      await page.keyboard.press("ArrowRight");
      await expect(tab).toBeFocused();
      await expect(
        page.getByRole("heading", { name: "pyfly", exact: true }),
      ).toBeVisible();
      const assertVisibleWidth = async (view: Page) => {
        const clipped = await view
          .locator(".framework-releases *")
          .evaluateAll((elements) =>
            elements
              .filter((element) => {
                const rect = element.getBoundingClientRect();
                return (
                  rect.width > 0 &&
                  (rect.left < -1 || rect.right > window.innerWidth + 1)
                );
              })
              .map((element) => element.textContent),
          );
        expect(clipped).toEqual([]);
      };
      await assertVisibleWidth(page);
      expect(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth <=
            document.documentElement.clientWidth + 1,
        ),
      ).toBe(true);
      await page.screenshot({
        path: test
          .info()
          .outputPath(`framework-releases-${width}-${colorScheme}.png`),
        fullPage: true,
      });
      await page
        .getByRole("article", { name: "pyfly", exact: true })
        .getByText("Release provenance", { exact: true })
        .click();
      await page
        .getByRole("article", { name: "pyfly", exact: true })
        .getByText("License SHA-256", { exact: true })
        .scrollIntoViewIfNeeded();
      await assertVisibleWidth(page);
      await page.screenshot({
        path: test
          .info()
          .outputPath(`framework-releases-${width}-${colorScheme}-details.png`),
        fullPage: true,
      });
      await page
        .getByRole("heading", { name: "larafly", exact: true })
        .scrollIntoViewIfNeeded();
      await page.screenshot({
        path: test
          .info()
          .outputPath(`framework-releases-${width}-${colorScheme}-larafly.png`),
        fullPage: true,
      });
      if (width === 1440) {
        // A 720 CSS-pixel layout at DPR2 measures a 1440px window's 200% reflow.
        const zoom = await browser.newContext({
          viewport: { width: 720, height: 500 },
          deviceScaleFactor: 2,
          colorScheme,
        });
        const view = await zoom.newPage();
        await factory(view);
        expect(
          await view.evaluate(() => [
            window.innerWidth,
            window.devicePixelRatio,
            matchMedia("(max-width:900px)").matches,
          ]),
        ).toEqual([720, 2, true]);
        await assertVisibleWidth(view);
        await view.screenshot({
          path: test
            .info()
            .outputPath(`framework-releases-${colorScheme}-reflow-200.png`),
          fullPage: true,
        });
        await view
          .getByRole("article", { name: "pyfly", exact: true })
          .getByText("Release provenance", { exact: true })
          .click();
        await view
          .getByRole("article", { name: "pyfly", exact: true })
          .getByText("License SHA-256", { exact: true })
          .scrollIntoViewIfNeeded();
        await assertVisibleWidth(view);
        await view.screenshot({
          path: test
            .info()
            .outputPath(
              `framework-releases-${colorScheme}-reflow-200-details.png`,
            ),
          fullPage: true,
        });
        await zoom.close();
      }
    });
  }
}

for (const colorScheme of ["light", "dark"] as const) {
  for (const width of [320, 1440]) {
    test(`generation activation keeps exact corpus and original identity at ${width}px ${colorScheme}`, async ({
      page,
    }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme });
      let activated:
        | import("../src/FrameworkKnowledgeData").FrameworkGenerationTarget
        | null = null;
      const writes: { id: string; body: unknown }[] = [];
      const reads: string[] = [];
      const runId = "3".repeat(32);
      await factory(page, async (route) => {
        const path = new URL(route.request().url()).pathname;
        if (path.endsWith("/frameworks/status")) {
          await route.fulfill({
            json: {
              version: 1,
              frameworks: [
                {
                  ...status,
                  current_corpus: corpus,
                  counts: completeCounts,
                  state: "current",
                  generation_target: activated,
                  discovery_retry_at: "2026-10-26T10:00:00Z",
                  discovery_error: "Controlled discovery backoff",
                  revision: activated ? "f".repeat(64) : status.revision,
                  allowed_actions: [
                    "browse",
                    "search",
                    "activate_generation_target",
                  ],
                },
                larafly,
              ],
            },
          });
          return true;
        }
        if (path.endsWith("/generation-candidates")) {
          const url = new URL(route.request().url());
          const selectedRef = url.searchParams.get("corpus_ref");
          expect([oldCorpus.corpus_ref, corpus.corpus_ref]).toContain(
            selectedRef,
          );
          expect(url.searchParams.get("page_size")).toBe("50");
          await route.fulfill({
            json: {
              version: 1,
              framework: "pyfly",
              corpus_ref: selectedRef,
              items:
                selectedRef === corpus.corpus_ref ? [generationCandidate] : [],
              next_cursor: null,
            },
          });
          return true;
        }
        const match = path.match(/\/generation-targets\/([a-f0-9]{32})$/);
        if (!match) return false;
        if (route.request().method() === "POST") {
          const body = route.request().postDataJSON();
          writes.push({ id: match[1], body });
          activated = {
            version: 1,
            request_id: match[1],
            framework: "pyfly",
            corpus,
            validation_run_id: runId,
            validation_source_tree: "4".repeat(40),
            validation_digest: "5".repeat(64),
            activation_actor_digest: "6".repeat(64),
            activated_at: "2026-10-26T10:10:00Z",
            revision: "9".repeat(64),
          };
          await route.abort("failed");
        } else {
          reads.push(match[1]);
          await route.fulfill({ json: activated });
        }
        return true;
      });
      await page
        .getByText("Review generation default", { exact: true })
        .click();
      await page
        .getByLabel("Framework release", { exact: true })
        .selectOption(oldCorpus.corpus_ref);
      await expect(
        page.getByText("No eligible validation runs found.", { exact: false }),
      ).toBeVisible();
      await expect(
        page.getByRole("button", { name: "Review exact target", exact: true }),
      ).toBeDisabled();
      await expect(
        page.getByLabel("Eligible validation run", { exact: true }),
      ).toBeDisabled();
      await page
        .getByLabel("Framework release", { exact: true })
        .selectOption(corpus.corpus_ref);
      const selectedCommit = page.locator(".framework-corpus-selector");
      await expect(
        selectedCommit.locator(".framework-selected-commit"),
      ).toHaveText("bbbbbbbbbbbb");
      const fullCommit = selectedCommit.getByText(corpus.commit, {
        exact: true,
      });
      await expect(fullCommit).not.toBeVisible();
      await selectedCommit
        .getByText("Full selected commit", { exact: true })
        .click();
      await expect(fullCommit).toBeVisible();
      await selectedCommit
        .getByText("Full selected commit", { exact: true })
        .click();
      await page
        .getByLabel("Eligible validation run", { exact: true })
        .selectOption(runId);
      await expect(
        page.getByRole("button", {
          name: "Activate reviewed target",
          exact: true,
        }),
      ).toHaveCount(0);
      await expect(
        page
          .getByRole("article", { name: "pyfly", exact: true })
          .locator(".framework-counts"),
      ).toHaveText("5 files · 1 documents · 1 examples · 1 API files");
      const generationSection = page.locator(".framework-generation-review");
      await focusedFrameworkCapture(
        page,
        generationSection,
        "framework-generation-candidates.png",
        width === 320
          ? {
              stage: "framework-generation-candidates",
              theme: colorScheme,
              device: "mobile",
              selector: ".framework-generation-review",
              actionSelector: ".framework-generation-review form button",
            }
          : undefined,
      );
      await page
        .getByRole("button", { name: "Review exact target", exact: true })
        .click();
      const review = page.getByRole("region", {
        name: "Generation activation review",
      });
      await expect(
        review.getByText(corpus.corpus_ref, { exact: true }),
      ).toBeVisible();
      await expect(review.getByText(runId, { exact: true })).toBeVisible();
      const overflow = await page
        .locator(".framework-generation-review")
        .evaluateAll((elements) =>
          elements
            .filter((element) => element.scrollWidth > element.clientWidth + 1)
            .map((element) => element.tagName),
        );
      expect(overflow).toEqual([]);
      const activateButton = page.getByRole("button", {
        name: "Activate reviewed target",
        exact: true,
      });
      await review.evaluate((element) =>
        element.scrollIntoView({ block: "center" }),
      );
      expect(
        (await activateButton.boundingBox())?.height,
      ).toBeGreaterThanOrEqual(40);
      await focusedFrameworkCapture(
        page,
        generationSection,
        "framework-generation-review.png",
        width === 1440
          ? {
              stage: "framework-generation-review",
              theme: colorScheme,
              device: "desktop",
              selector: ".framework-generation-review",
              actionSelector: ".framework-activation-confirm button.primary",
            }
          : undefined,
      );
      await focusedFrameworkCapture(
        page,
        review,
        "framework-generation-confirmation.png",
        width === 320
          ? {
              stage: "framework-generation-confirmation",
              theme: colorScheme,
              device: "mobile",
              selector: ".framework-activation-confirm",
              actionSelector: ".framework-activation-confirm button.primary",
            }
          : undefined,
      );
      await frameworkContextCapture(page, "framework-generation-context.png");
      await page
        .getByRole("button", { name: "Activate reviewed target", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Unconfirmed generation activation",
          exact: true,
        }),
      ).toBeVisible();
      expect(writes).toHaveLength(1);
      expect(writes[0].body).toEqual({
        version: 1,
        corpus_ref: corpus.corpus_ref,
        expected_status_revision: status.revision,
        validation_run_id: runId,
      });
      await page.reload();
      await navigate(page, "Knowledge");
      await page
        .getByRole("tab", { name: "Framework releases", exact: true })
        .click();
      await expect(page.getByText(writes[0].id, { exact: true })).toBeVisible();
      await expect(
        page.getByText("Review generation default", { exact: true }),
      ).toHaveCount(0);
      await page
        .getByRole("button", { name: "Read original activation", exact: true })
        .click();
      await expect(
        page.getByRole("heading", {
          name: "Unconfirmed generation activation",
          exact: true,
        }),
      ).toHaveCount(0);
      const target = page.getByRole("region", {
        name: "pyfly generation default",
      });
      await expect(target).toContainText("26.10.11");
      await target.getByText("Activation provenance", { exact: true }).click();
      await expect(target.getByText(runId, { exact: true })).toBeVisible();
      await focusedFrameworkCapture(
        page,
        target,
        "framework-generation-activated.png",
      );
      await focusedFrameworkCapture(
        page,
        target.locator(".framework-provenance"),
        "framework-generation-activated-provenance.png",
      );
      expect(reads).toEqual([writes[0].id]);
      expect(writes).toHaveLength(1);
    });
  }
}

test("legacy documents-only corpora stay ineligible while complete-corpus candidates paginate, recover reads and clear review", async ({
  page,
}) => {
  const runId = "3".repeat(32);
  const cursor = "7".repeat(32);
  let failedPage = false;
  let finishFirstRead!: () => void;
  const firstRead = new Promise<void>((resolve) => {
    finishFirstRead = resolve;
  });
  const reads: string[] = [];
  const writes: string[] = [];
  await factory(page, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/frameworks/status")) {
      await route.fulfill({
        json: {
          version: 1,
          frameworks: [
            {
              ...status,
              current_corpus: oldCorpus,
              generation_target: null,
              allowed_actions: [
                "browse",
                "search",
                "activate_generation_target",
              ],
            },
          ],
        },
      });
      return true;
    }
    if (route.request().method() === "POST") writes.push(url.pathname);
    if (!url.pathname.endsWith("/generation-candidates")) return false;
    expect(route.request().method()).toBe("GET");
    const ref = url.searchParams.get("corpus_ref");
    if (ref === corpus.corpus_ref && !url.searchParams.has("cursor"))
      await firstRead;
    reads.push(`${ref}:${url.searchParams.get("cursor") ?? "first"}`);
    if (
      ref === corpus.corpus_ref &&
      url.searchParams.get("cursor") === cursor &&
      !failedPage
    ) {
      failedPage = true;
      await route.fulfill({
        status: 503,
        json: { detail: "Controlled candidate read unavailable" },
      });
      return true;
    }
    await route.fulfill({
      json: {
        version: 1,
        framework: "pyfly",
        corpus_ref: ref,
        items:
          ref === corpus.corpus_ref && url.searchParams.get("cursor") === cursor
            ? [generationCandidate]
            : [],
        next_cursor:
          ref === corpus.corpus_ref && !url.searchParams.has("cursor")
            ? cursor
            : null,
      },
    });
    return true;
  });
  await page.getByText("Review generation default", { exact: true }).click();
  const reviewButton = page.getByRole("button", {
    name: "Review exact target",
    exact: true,
  });
  await expect(reviewButton).toBeDisabled();
  await page
    .getByLabel("Framework release", { exact: true })
    .selectOption(corpus.corpus_ref);
  await expect(
    page.getByText("Reading eligible validation runs…", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Eligible validation run", { exact: true }),
  ).toBeDisabled();
  finishFirstRead();
  await expect(
    page.getByText("No eligible validation runs on this page.", {
      exact: false,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Load more validation runs", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Controlled candidate read unavailable",
  );
  await page
    .getByRole("button", { name: "Retry loading more runs", exact: true })
    .click();
  const select = page.getByLabel("Eligible validation run", { exact: true });
  await expect(
    select.getByRole("option", { name: /inventory-service \/ api/ }),
  ).toHaveCount(1);
  await select.selectOption(runId);
  await expect(page.getByText("python 3.13.13", { exact: true })).toBeVisible();
  await select.focus();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("button", { name: "Reload validation runs", exact: true }),
  ).toBeFocused();
  await reviewButton.click();
  await expect(
    page.getByRole("region", { name: "Generation activation review" }),
  ).toBeVisible();
  await page
    .getByLabel("Framework release", { exact: true })
    .selectOption(oldCorpus.corpus_ref);
  await expect(
    page.getByRole("region", { name: "Generation activation review" }),
  ).toHaveCount(0);
  await expect(select).toHaveValue("");
  await expect(select).toBeDisabled();
  await expect(reviewButton).toBeDisabled();
  await expect(
    page.getByText("No eligible validation runs found.", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Reload validation runs", exact: true })
    .click();
  await expect(
    page.getByText("No eligible validation runs found.", { exact: false }),
  ).toBeVisible();
  expect(reads.filter((value) => value.endsWith(`:${cursor}`))).toEqual([
    `${corpus.corpus_ref}:${cursor}`,
    `${corpus.corpus_ref}:${cursor}`,
  ]);
  expect(new Set(reads)).toEqual(
    new Set([
      `${corpus.corpus_ref}:first`,
      `${corpus.corpus_ref}:${cursor}`,
      `${oldCorpus.corpus_ref}:first`,
    ]),
  );
  expect(writes).toEqual([]);
});

test("refresh review exposes a keyboard reachable touch action and transitions to retained progress across screen sizes", async ({
  browser,
}, info) => {
  test.setTimeout(90_000);
  for (const colorScheme of ["light", "dark"] as const)
    for (const viewport of [
      { width: 320, height: 568 },
      { width: 390, height: 844 },
      { width: 1440, height: 900 },
    ]) {
      const context = await browser.newContext({
        baseURL: info.project.use.baseURL,
        viewport,
        colorScheme,
      });
      try {
        await context.route("**/*", (route) => {
          const url = new URL(route.request().url());
          return url.origin === new URL(info.project.use.baseURL!).origin &&
            !url.pathname.startsWith("/api")
            ? route.continue()
            : route.abort("blockedbyclient");
        });
        const page = await context.newPage();
        await factory(page, async (route) => {
          const match = new URL(route.request().url()).pathname.match(
            /\/refreshes\/([a-f0-9]{32})$/,
          );
          if (!match) return false;
          await route.fulfill({ json: operation(match[1]) });
          return true;
        });
        await page
          .getByRole("article", { name: "pyfly", exact: true })
          .getByRole("button", { name: "Check updates", exact: true })
          .click();
        const refresh = page.getByRole("button", {
          name: "Start refresh",
          exact: true,
        });
        await refresh.scrollIntoViewIfNeeded();
        await expect(refresh).toBeEnabled();
        const box = await refresh.boundingBox();
        expect
          .soft(box!.height, `${viewport.width} ${colorScheme} touch target`)
          .toBeGreaterThanOrEqual(40);
        await refresh.focus();
        await page.keyboard.press("Tab");
        await page.keyboard.press("Shift+Tab");
        await expect(refresh).toBeFocused();
        const focusedRingGap = await refresh.evaluate((element) => {
          const paragraph = element
            .closest(".framework-refresh-review")!
            .querySelector("p:last-of-type")!
            .getBoundingClientRect();
          const style = getComputedStyle(element);
          return (
            element.getBoundingClientRect().top -
            parseFloat(style.outlineOffset) -
            parseFloat(style.outlineWidth) -
            paragraph.bottom
          );
        });
        await info.attach(
          `refresh-focused-gap-${viewport.width}-${colorScheme}`,
          {
            body: JSON.stringify({ viewport, colorScheme, focusedRingGap }),
            contentType: "application/json",
          },
        );
        expect
          .soft(
            focusedRingGap,
            `${viewport.width} ${colorScheme} paragraph to focused ring`,
          )
          .toBeGreaterThanOrEqual(8);
        expect
          .soft(
            await refresh.evaluate((element) => {
              const style = getComputedStyle(element);
              const box = element.getBoundingClientRect();
              const footer = document
                .querySelector(".workspace-main > footer")!
                .getBoundingClientRect();
              return (
                style.outlineStyle !== "none" &&
                parseFloat(style.outlineWidth) >= 2 &&
                box.top >= 0 &&
                box.bottom <= footer.top &&
                box.left >= 0 &&
                box.right <= innerWidth &&
                document
                  .elementFromPoint(
                    box.x + box.width / 2,
                    box.y + box.height / 2,
                  )
                  ?.closest("button") === element
              );
            }),
          )
          .toBe(true);
        await page.screenshot({
          path: info.outputPath(`refresh-${viewport.width}-${colorScheme}.png`),
        });
        await page.keyboard.press("Enter");
        await expect(
          page.getByRole("heading", { name: /Refresh target 26.10.11/ }),
        ).toBeVisible();
        await expect(refresh).toHaveCount(0);
        expect(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
        ).toBe(true);
      } finally {
        await context.close();
      }
    }
});
