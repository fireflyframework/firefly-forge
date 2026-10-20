/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test, type Page } from "@playwright/test";
import packet from "./scm-poll-resume-contract-fixture.json" with { type: "json" };
import { setupDelegation, projectCut, digest } from "./scm-delegation-fixture";
import { navigate } from "./navigation";
import type {
  PollResumeInput,
  PollResumeReceipt,
} from "../src/ScmPollResumeData";
import type { PollingBindingReceipt } from "../src/ScmPollingData";
// Controlled transport responses exercise the UI; native authorization has separate database tests.
async function setup(page: Page, reader = false) {
  const base = await setupDelegation(page, { reader });
  const value = structuredClone(packet.binding);
  value.project_binding_digest = projectCut;
  value.request.expected_project_binding_digest = projectCut;
  value.request_digest = digest(value.request);
  const { digest: _old, ...rest } = value;
  void _old;
  const binding = { ...rest, digest: digest(rest) } as PollingBindingReceipt;
  const state = {
    posts: [] as PollResumeInput[],
    receipts: new Map<string, PollResumeReceipt>(),
    current: null as PollResumeReceipt | null,
    heads: [binding],
    loseResponse: false,
    postStatus: 201,
    exactStatus: 200,
    markers: [] as unknown[][],
  };
  await page.route("**/scm-polling/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.includes("/resume-grants")) {
      if (req.method() === "POST") {
        const body = req.postDataJSON() as PollResumeInput;
        state.posts.push(body);
        state.markers.push(
          await page.evaluate(() =>
            Object.entries(localStorage)
              .filter(([key]) =>
                key.startsWith("forge.scm-poll-resume-consent.v3:"),
              )
              .map(([, raw]) => JSON.parse(raw)),
          ),
        );
        const unsigned = {
          ...packet.receipt,
          event_id: "a".repeat(32),
          administering_user_id: base.actor,
          binding_key: binding.binding_key,
          native_user_id: binding.request.native_user_id,
          request: body,
          request_digest: digest(body),
          previous_digest: body.expected_previous_digest,
          revision: body.expected_previous_digest ? 2 : 1,
        };
        const { digest: _ignore, ...fields } = unsigned;
        void _ignore;
        const receipt = {
          ...fields,
          digest: digest(fields),
        } as PollResumeReceipt;
        if (state.postStatus === 201) {
          state.receipts.set(body.request_id, receipt);
          state.current = receipt;
        }
        if (state.loseResponse) return route.abort("failed");
        return route.fulfill({
          status: state.postStatus,
          json:
            state.postStatus === 201
              ? receipt
              : { detail: "Current consent changed" },
        });
      }
      const exact = path.includes("/requests/"),
        value = exact
          ? state.receipts.get(path.split("/").at(-1)!)
          : state.current;
      const status =
        exact && state.exactStatus !== 200
          ? state.exactStatus
          : value
            ? 200
            : 404;
      return route.fulfill({
        status,
        json: status === 200 ? value : { detail: "No exact receipt" },
      });
    }
    if (path.includes("/bindings/requests/"))
      return route.fulfill({ json: binding });
    return route.fulfill({
      json: {
        project_id: binding.project_id,
        repository_id: binding.repository_id,
        heads_only: true,
        items: state.heads,
        next_after: null,
      },
    });
  });
  async function select() {
    await page
      .getByRole("button", { name: "Read GitHub accounts", exact: true })
      .click();
    await page
      .getByLabel("GitHub continuation account", { exact: true })
      .selectOption(binding.request.request_id);
    await expect(
      page.getByRole("region", {
        name: "GitHub continuation consent",
        exact: true,
      }),
    ).toBeVisible();
  }
  await select();
  return { state, base, binding, select };
}
async function review(page: Page) {
  await page
    .getByRole("button", { name: "Review GitHub continuation", exact: true })
    .click();
  const modal = page.getByRole("dialog", {
    name: "Review GitHub continuation consent",
    exact: true,
  });
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    modal.getByRole("checkbox", { name: "Inventory API", exact: true }),
  ).toBeChecked();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await modal
    .getByRole("checkbox", { name: /I approve this GitHub account/ })
    .check();
  return modal;
}
test("polling consent records one exact request before POST without signed consent writes", async ({
  page,
}) => {
  const { state, base } = await setup(page);
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(
    page.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
  expect(state.posts[0].transport).toBe("github_rest_poll_v1");
  expect(state.posts[0].allowed_actions).toEqual(["resume"]);
  expect(state.markers[0]).toHaveLength(1);
  expect(state.markers[0][0]).not.toHaveProperty("component_ids");
  expect(base.posts).toHaveLength(0);
});
test("a lost polling consent response survives reload and exact read without another POST", async ({
  page,
}) => {
  const { state, select } = await setup(page);
  state.loseResponse = true;
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("unconfirmed");
  await page.reload();
  await navigate(page, "Project access");
  await select();
  await expect(
    page.getByRole("button", {
      name: "Review GitHub continuation",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(
    page.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts).toHaveLength(1);
});
test("a changed polling head stops final consent before any write", async ({
  page,
}) => {
  const { state } = await setup(page);
  const modal = await review(page);
  state.heads = [];
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "current active GitHub account",
  );
  expect(state.posts).toHaveLength(0);
});
test("project readers can inspect polling consent without managing it", async ({
  page,
}) => {
  const { state } = await setup(page, true);
  await expect(
    page.getByRole("button", {
      name: "Read current GitHub consent",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "Review GitHub continuation",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toHaveLength(0);
});
test("continuation permission can be revoked through a new explicit revision", async ({
  page,
}) => {
  const { state } = await setup(page);
  const first = await review(page);
  await first
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(
    page.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  const previous = state.current!.digest;
  await page
    .getByRole("button", { name: "Review GitHub continuation", exact: true })
    .click();
  const modal = page.getByRole("dialog", {
    name: "Review GitHub continuation consent",
    exact: true,
  });
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await modal
    .getByRole("checkbox", {
      name: /Allow this person to continue answered work/,
    })
    .uncheck();
  await modal.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    modal.getByRole("heading", {
      name: "Turn off GitHub continuation",
      exact: true,
    }),
  ).toBeVisible();
  await modal
    .getByRole("checkbox", { name: /I approve this GitHub account/ })
    .check();
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect.poll(() => state.posts.length).toBe(2);
  await expect(
    page.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.posts[1].active).toBe(false);
  expect(state.posts[1].expected_previous_digest).toBe(previous);
  expect(state.posts[1].request_id).not.toBe(state.posts[0].request_id);
});
test("a conflicting consent POST retains the original and blocks a replacement", async ({
  page,
}) => {
  const { state } = await setup(page);
  state.postStatus = 409;
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("unconfirmed");
  await page
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("unconfirmed");
  await expect(
    page.getByRole("button", {
      name: "Review GitHub continuation",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(1);
  expect(state.markers[0]).toHaveLength(1);
});
test("access loss clears private receipt details but retains the original request", async ({
  page,
}) => {
  const { state } = await setup(page);
  state.loseResponse = true;
  const modal = await review(page);
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("unconfirmed");
  state.exactStatus = 403;
  await page
    .getByRole("button", { name: "Check original consent 1", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Private consent details have been cleared",
  );
  await expect(
    page.getByRole("region", {
      name: "Exact resume consent receipt",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", {
      name: "Review GitHub continuation",
      exact: true,
    }),
  ).toBeDisabled();
  expect(state.posts).toHaveLength(1);
});
test("a browser without Web Locks cannot send continuation consent", async ({
  page,
}) => {
  const { state } = await setup(page);
  const modal = await review(page);
  await page.evaluate(() =>
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: undefined,
    }),
  );
  await modal
    .getByRole("button", { name: "Save permission", exact: true })
    .click();
  await expect(
    page.getByText(
      /This browser cannot coordinate consent requests across tabs/,
    ),
  ).toBeVisible();
  expect(state.posts).toHaveLength(0);
});
for (const viewport of [
  { width: 1440, height: 900, zoom: 1 },
  { width: 390, height: 600, zoom: 1 },
  { width: 320, height: 400, zoom: 1 },
  { width: 1440, height: 1000, zoom: 2 },
])
  for (const theme of ["light", "dark"])
    test(`polling consent fits ${viewport.width}x${viewport.height} zoom${viewport.zoom} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      if (viewport.zoom === 2)
        await page.evaluate(() => (document.documentElement.style.zoom = "2"));
      const modal = await review(page);
      const ack = modal.getByRole("checkbox", {
        name: /I approve this GitHub account/,
      });
      await ack.focus();
      await page.keyboard.press("Space");
      await page.keyboard.press("Space");
      await expect(ack).toBeChecked();
      const bounds = await modal.evaluate((node) => {
        const r = node.getBoundingClientRect();
        return {
          left: r.left,
          right: r.right,
          top: r.top,
          bottom: r.bottom,
          overflow: document.documentElement.scrollWidth > innerWidth,
        };
      });
      expect(bounds.left).toBeGreaterThanOrEqual(0);
      expect(bounds.top).toBeGreaterThanOrEqual(0);
      expect(bounds.right).toBeLessThanOrEqual(viewport.width);
      expect(bounds.bottom).toBeLessThanOrEqual(viewport.height);
      expect(bounds.overflow).toBe(false);
      const action = modal.getByRole("button", {
        name: "Save permission",
        exact: true,
      });
      await action.scrollIntoViewIfNeeded();
      await expect(action).toBeInViewport();
      expect(
        await action.evaluate((el) => {
          const r = el.getBoundingClientRect();
          return el.contains(
            document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2),
          );
        }),
      ).toBe(true);
      await page.screenshot({
        path: info.outputPath(
          `poll-consent-${viewport.width}-${theme}-zoom${viewport.zoom}.png`,
        ),
      });
      await page.keyboard.press("Escape");
      await expect(modal).not.toBeVisible();
    });
