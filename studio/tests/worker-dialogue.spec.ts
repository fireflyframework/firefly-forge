/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, test, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import contract from "./worker-dialogue-contract-fixture.json" with { type: "json" };
import delegation from "./scm-delegation-contract-fixture.json" with { type: "json" };

const run = {
  id: "2".repeat(32),
  repository_id: "3".repeat(32),
  project_id: null,
  brief: "Inventory decision fixture",
  status: "paused",
  publish: false,
};
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(row[key])}`)
    .join(",")}}`;
}
function seal(body: Record<string, unknown>) {
  return {
    ...body,
    digest: createHash("sha256").update(canonical(body)).digest("hex"),
  };
}
const feedback = contract.feedback;
async function setup(
  page: Page,
  lost = false,
  delegated?: { deny?: boolean; delay?: boolean },
) {
  const viewedRun = delegated
    ? {
        ...run,
        project_id: delegation.receipt.project_id,
        repository_id: delegation.receipt.repository_id,
        component_id: "api",
      }
    : run;
  let releaseBinding = () => {};
  const bindingGate = delegated?.delay
    ? new Promise<void>((resolve) => {
        releaseBinding = resolve;
      })
    : Promise.resolve();
  const state = {
    posts: 0,
    reads: 0,
    events: [feedback] as Record<string, unknown>[],
    deny: false,
    bindingReads: 0,
    bindingDenied: delegated?.deny ?? false,
    releaseBinding,
  };
  if (delegated) {
    const { digest: _digest, ...event } = contract.legacy_response;
    void _digest;
    const reference = delegation.receipt.verified_reference;
    state.events.push(
      seal({
        ...event,
        payload: {
          ...event.payload,
          actor: "7",
          responding_actor: {
            version: 2,
            user_id: delegation.request.native_user_id,
            authentication_method: "scm_mapping",
            provenance: {
              binding_request_id: delegation.request.request_id,
              binding_digest: delegation.receipt.digest,
              verified_receipt_id: "3".repeat(32),
              provider: reference.provider,
              api_origin: reference.api_origin,
              connection_revision: reference.connection_revision,
              installation_id: reference.installation_id,
              remote_repository_id: reference.remote_repository_id,
              external_actor_id: reference.external_actor_id,
            },
          },
        },
      }),
    );
  }
  await page.context().route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.includes("/scm-actor-bindings/requests/")) {
      expect(route.request().method()).toBe("GET");
      expect(path).toBe(
        `/api/v1/projects/${delegation.receipt.project_id}/repositories/${delegation.receipt.repository_id}/scm-actor-bindings/requests/${delegation.request.request_id}`,
      );
      state.bindingReads++;
      await bindingGate;
      return route.fulfill(
        state.bindingDenied
          ? { status: 403, json: { detail: "private-mapping-denial-canary" } }
          : { json: delegation.receipt },
      );
    }
    if (path.endsWith("/respond")) {
      state.posts++;
      const body = route.request().postDataJSON();
      const markers = await page.evaluate(() =>
        Object.entries(localStorage).filter(([key]) =>
          key.startsWith("forge.worker-reply.v1:"),
        ),
      );
      expect(markers).toHaveLength(1);
      expect(markers[0][1]).not.toContain(body.answer);
      expect(markers[0][1]).toContain(body.id);
      state.events.push(
        seal({
          run_id: run.id,
          sequence: 4,
          task_id: "implement",
          idempotency_key: `worker-response:${body.id}`,
          kind: "worker_response",
          created_at: "2026-10-15T12:02:00+00:00",
          previous_hash: "b".repeat(64),
          payload: { ...body, request_id: "reservation-limit" },
        }),
      );
      if (lost) return route.abort("failed");
      return route.fulfill({ json: state.events[1] });
    }
    if (path.endsWith("/worker-messages")) {
      state.reads++;
      if (state.deny)
        return route.fulfill({
          status: 403,
          json: { detail: "private-denial-canary" },
        });
      return route.fulfill({ json: state.events });
    }
    const json = path.endsWith("/repositories")
      ? [
          {
            id: viewedRun.repository_id,
            name: "Inventory",
            url: "https://example.test/inventory.git",
            provider: "git",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [viewedRun]
        : path.endsWith(`/runs/${run.id}`)
          ? viewedRun
          : path.endsWith("/health")
            ? { status: "ok", execution_ready: false }
            : [];
    return route.fulfill({ json });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await page.getByRole("tab", { name: "Agent questions", exact: true }).click();
  if (!delegated)
    await expect(
      page.getByRole("heading", {
        name: "What is the maximum reservation?",
        exact: true,
      }),
    ).toBeVisible();
  return state;
}
test("review, retain one identity, send once and confirm the recorded answer without resuming", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "14 items", exact: true }).click();
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Review your answer", exact: true }),
  ).toBeFocused();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText(
      "Answer recorded. The run stays paused until you resume it.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
  expect(state.reads).toBeGreaterThan(1);
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toHaveCount(0);
});
test("a lost delivery keeps its original identity and reload reconciles with GET only", async ({
  page,
}) => {
  const state = await setup(page, true);
  await page
    .getByLabel("Your answer", { exact: true })
    .fill("Reserve at most 14 items.\nKeep the operation atomic.");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Check original answer", exact: true }),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await page.getByRole("tab", { name: "Agent questions", exact: true }).click();
  await expect(
    page.getByText(
      "Answer recorded. The run stays paused until you resume it.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(1);
});

test("missing readback and permission refusal never create a replacement answer or reflect private errors", async ({
  page,
}) => {
  const state = await setup(page, true);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Check original answer", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "Delivery is unconfirmed. Check the original answer; another answer will not be sent automatically.",
      { exact: true },
    ),
  ).toBeVisible();
  state.events = [feedback];
  await page
    .getByRole("button", { name: "Check original answer", exact: true })
    .click();
  await expect(
    page.getByText(
      "No matching answer is currently available. Keep the original request; this does not prove it was not sent.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByLabel("Your answer", { exact: true })).toHaveCount(0);
  state.deny = true;
  await page
    .getByRole("button", { name: "Check original answer", exact: true })
    .click();
  await expect(
    page.getByText(
      "Your current session cannot read this conversation. Sign in or ask the project owner to review your access.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(page.getByText("private-denial-canary")).toHaveCount(0);
  await expect(
    page.getByRole("heading", {
      name: "What is the maximum reservation?",
      exact: true,
    }),
  ).toHaveCount(0);
  expect(state.posts).toBe(1);
});

test("unavailable durable tracking refuses the write before dispatch", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new Error("storage unavailable");
    };
  });
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText(
      "The answer identity could not be safely retained. Nothing further will be sent from this form; review the original conversation before continuing.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(state.posts).toBe(0);
});

async function holdReplyScope(page: Page): Promise<string> {
  return page.evaluate(
    async ({ actor, repository, runId }) => {
      const base = new URL("/api/v1", location.origin).href;
      const name = `forge.worker-reply.v1:${encodeURIComponent(base)}:${actor}:unassigned:${repository}:${runId}:`;
      let acquired!: () => void;
      const ready = new Promise<void>((resolve) => {
        acquired = resolve;
      });
      const gate = new Promise<void>((resolve) => {
        (
          window as typeof window & { releaseReplyClaim: () => void }
        ).releaseReplyClaim = resolve;
      });
      void navigator.locks.request(name, { mode: "exclusive" }, async () => {
        acquired();
        await gate;
      });
      await ready;
      return name;
    },
    { actor: "f".repeat(32), repository: run.repository_id, runId: run.id },
  );
}
async function releaseReplyScope(page: Page): Promise<void> {
  await page.evaluate(() => {
    (
      window as typeof window & { releaseReplyClaim: () => void }
    ).releaseReplyClaim();
  });
}
async function pendingReplyClaims(page: Page, name: string): Promise<number> {
  return page.evaluate(
    async (name) =>
      (await navigator.locks.query()).pending!.filter(
        (lock) => lock.name === name,
      ).length,
    name,
  );
}

test("two open tabs retain one original answer before either can dispatch", async ({
  page,
}) => {
  const state = await setup(page);
  const second = await page.context().newPage();
  await second.goto("/");
  await second
    .getByRole("button", { name: /Inventory decision fixture/ })
    .click();
  await second
    .getByRole("tab", { name: "Agent questions", exact: true })
    .click();
  for (const tab of [page, second]) {
    await tab.getByLabel("Your answer", { exact: true }).fill("14 items");
    await tab
      .getByRole("button", { name: "Review answer", exact: true })
      .click();
  }
  const lockName = await holdReplyScope(page);
  await Promise.all(
    [page, second].map((tab) =>
      tab.getByRole("button", { name: "Send answer", exact: true }).click(),
    ),
  );
  await expect.poll(() => pendingReplyClaims(page, lockName)).toBe(2);
  expect(state.posts).toBe(0);
  expect(
    await page.evaluate(() =>
      Object.keys(localStorage).filter((key) =>
        key.startsWith("forge.worker-reply.v1:"),
      ),
    ),
  ).toHaveLength(0);
  await releaseReplyScope(page);
  await expect.poll(() => state.posts).toBe(1);
  await expect
    .poll(async () =>
      (
        await Promise.all(
          [page, second].map(async (tab) => {
            const button = tab.getByRole("button", {
              name: "Send answer",
              exact: true,
            });
            return (await button.count())
              ? (await button.isDisabled())
                ? "held"
                : "active"
              : "recorded";
          }),
        )
      ).sort(),
    )
    .toEqual(["held", "recorded"]);
  expect(state.posts).toBe(1);
});

test("a queued reply claim cannot retain or dispatch after navigation changes its context", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  const lockName = await holdReplyScope(page);
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect.poll(() => pendingReplyClaims(page, lockName)).toBe(1);
  await navigate(page, "Connections");
  await expect(
    page.getByRole("heading", { name: "Connect the factory", exact: true }),
  ).toBeVisible();
  await releaseReplyScope(page);
  await expect.poll(() => pendingReplyClaims(page, lockName)).toBe(0);
  await expect
    .poll(() =>
      page.evaluate(
        async (name) =>
          (await navigator.locks.query()).held!.filter(
            (lock) => lock.name === name,
          ).length,
        lockName,
      ),
    )
    .toBe(0);
  expect(
    await page.evaluate(() =>
      Object.keys(localStorage).filter((key) =>
        key.startsWith("forge.worker-reply.v1:"),
      ),
    ),
  ).toHaveLength(0);
  expect(state.posts).toBe(0);
});

test("missing browser coordination holds the original form without storage or dispatch", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.evaluate(() =>
    Object.defineProperty(navigator, "locks", { value: undefined }),
  );
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "Agent questions and answers", exact: true })
      .getByRole("alert"),
  ).toHaveText(
    "This browser cannot safely coordinate answer requests across tabs. Use a supported browser on HTTPS or localhost, then check the original conversation before continuing. No answer was sent.",
  );
  expect(
    await page.evaluate(() =>
      Object.keys(localStorage).filter((key) =>
        key.startsWith("forge.worker-reply.v1:"),
      ),
    ),
  ).toHaveLength(0);
  expect(state.posts).toBe(0);
});

test("a changed confirmed marker holds the next question before review or another POST", async ({
  page,
}) => {
  const state = await setup(page);
  await page.getByLabel("Your answer", { exact: true }).fill("14 items");
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(
    page.getByText(
      "Answer recorded. The run stays paused until you resume it.",
      { exact: true },
    ),
  ).toBeVisible();
  state.events.push(
    seal({
      run_id: run.id,
      sequence: 5,
      task_id: "implement",
      idempotency_key: "worker-feedback:next",
      kind: "worker_feedback",
      created_at: "2026-10-15T12:03:00+00:00",
      previous_hash: state.events[1].digest,
      payload: {
        ...contract.feedback.payload,
        id: "next",
        attempt: 2,
        request: {
          ...contract.feedback.payload.request,
          id: "next-question",
          question: "How should empty reservations behave?",
        },
      },
    }),
  );
  await page
    .getByRole("button", { name: "Check original answer", exact: true })
    .click();
  await page
    .getByLabel("Your answer", { exact: true })
    .fill("Reject an empty reservation.");
  await page.evaluate(() => {
    const key = Object.keys(localStorage).find((key) =>
      key.startsWith("forge.worker-reply.v1:"),
    )!;
    const marker = JSON.parse(localStorage.getItem(key)!);
    marker.inputDigest = "0".repeat(64);
    localStorage.setItem(key, JSON.stringify(marker));
  });
  await page
    .getByRole("button", { name: "Review answer", exact: true })
    .click();
  await expect(
    page.getByText(
      "An earlier answer request is unresolved or has changed. Check its original result before reviewing another answer.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toBe(1);
});

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 600 },
  { width: 320, height: 400 },
]) {
  for (const theme of ["light", "dark"]) {
    test(`answer review fits ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await setup(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await page
        .getByLabel("Your answer", { exact: true })
        .fill("Reserve up to **14 items**.\n\nKeep the reservation atomic.");
      await page
        .getByRole("button", { name: "Review answer", exact: true })
        .click();
      const send = page.getByRole("button", {
        name: "Send answer",
        exact: true,
      });
      await send.scrollIntoViewIfNeeded();
      await expect(send).toBeInViewport();
      const box = await send.boundingBox();
      expect(box).not.toBeNull();
      const footer = await page
        .locator(".workspace-main > footer")
        .boundingBox();
      if (box && footer)
        expect(box.y + box.height).toBeLessThanOrEqual(footer.y + 1);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath("answer-review.png"),
      });
    });
  }
}

test("delegated historical answers read one exact mapping without a write or personal-login claim", async ({
  page,
}) => {
  const state = await setup(page, false, {});
  await expect(
    page.getByText("Delegated via GitHub to a mapped Forge user.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recorded answer", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "The server also recorded the authenticated user identity.",
      { exact: false },
    ),
  ).toHaveCount(0);
  expect(state.bindingReads).toBe(1);
  expect(state.posts).toBe(0);
  const attribution = page.getByText(
    "Delegated via GitHub to a mapped Forge user.",
    { exact: false },
  );
  await attribution.scrollIntoViewIfNeeded();
  const bounds = await attribution.boundingBox();
  const footer = await page.getByRole("contentinfo").boundingBox();
  expect(bounds).not.toBeNull();
  expect(footer).not.toBeNull();
  expect(bounds!.y).toBeGreaterThanOrEqual(120);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(footer!.y);
  await page.screenshot({
    path: test.info().outputPath("delegated-answer.png"),
    fullPage: true,
  });
  await expect(
    page.getByRole("button", { name: "Review answer", exact: true }),
  ).toHaveCount(0);
});

test("denied exact delegation reads clear private conversation without another answer", async ({
  page,
}) => {
  const state = await setup(page, false, { deny: true });
  await expect(
    page.getByText(
      "Your current session cannot read this conversation. Sign in or ask the project owner to review your access.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Recorded answer", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByText("private-mapping-denial-canary")).toHaveCount(0);
  expect(state.bindingReads).toBe(1);
  expect(state.posts).toBe(0);
});

test("late historical mapping responses cannot repaint a different view", async ({
  page,
}) => {
  const state = await setup(page, false, { delay: true });
  await expect.poll(() => state.bindingReads).toBe(1);
  await navigate(page, "Connections");
  await expect(
    page.getByRole("heading", { name: "Connect the factory", exact: true }),
  ).toBeVisible();
  const delivery = page.waitForResponse((response) =>
    response.url().includes("/scm-actor-bindings/requests/"),
  );
  state.releaseBinding();
  await (await delivery).finished();
  await page.evaluate(
    () => new Promise((resolve) => requestAnimationFrame(() => resolve(null))),
  );
  await expect(
    page.getByText("Delegated via GitHub", { exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Recorded answer", exact: true }),
  ).toHaveCount(0);
  expect(state.posts).toBe(0);
});
