/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import {
  expect,
  test,
  type Locator,
  type Page,
  type TestInfo,
} from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate, expectAuthenticated } from "./navigation";
import contract from "./worker-dialogue-contract-fixture.json" with { type: "json" };

const runId = "2".repeat(32),
  repositoryId = "3".repeat(32),
  handoffId = "a".repeat(32),
  requestId = "verification-needed";
const question = "Can forge run the approved verification checks?";
const answerText = "Seven approved commands completed; all seven passed.";

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const row = value as Record<string, unknown>;
  return `{${Object.keys(row)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonical(row[key])}`)
    .join(",")}}`;
}
async function seal(body: Record<string, unknown>) {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(canonical(body)),
  );
  return {
    ...body,
    digest: [...new Uint8Array(bytes)]
      .map((value) => value.toString(16).padStart(2, "0"))
      .join(""),
  };
}

async function fixture(page: Page) {
  // Fixture API events use the native canonical JSON digest contract.
  const { digest: _questionDigest, ...questionBody } = contract.feedback;
  const { digest: _answerDigest, ...answerBody } = contract.legacy_response;
  void _questionDigest;
  void _answerDigest;
  const feedback = await seal({
    ...questionBody,
    idempotency_key: "worker-feedback:verification-feedback",
    payload: {
      ...questionBody.payload,
      id: "verification-feedback",
      summary:
        "The coding agent needs results from the approved project checks.",
      request: {
        id: requestId,
        kind: "verification",
        question,
        context:
          "The project already defines the checks needed by the coding agent.",
        choices: [],
      },
    },
  });
  const factoryAnswer = await seal({
    ...answerBody,
    sequence: 6,
    idempotency_key: `worker-response:${handoffId}`,
    payload: {
      id: handoffId,
      actor: "Forge verification",
      answer: answerText,
      request_id: requestId,
      source: "factory_verification",
      verification_handoff_id: handoffId,
      responding_actor: {
        version: 4,
        authentication_method: "factory_verification",
        handoff_id: handoffId,
        source_digest: "1".repeat(64),
        commands_digest: "2".repeat(64),
        policy_digest: "3".repeat(64),
        provider_digest: "4".repeat(64),
      },
    },
  });
  const state = {
    phase: "pending" as "pending" | "reconciliation" | "held" | "answered",
    mutations: [] as string[],
    conversationReads: 0,
    runReads: [] as string[],
  };
  function currentRun() {
    const answered = state.phase === "answered";
    return {
      id: runId,
      repository_id: repositoryId,
      project_id: null,
      brief: "Automatic verification UI fixture",
      agent: "codex",
      publish: false,
      status: answered
        ? "queued"
        : state.phase === "reconciliation"
          ? "needs_reconciliation"
          : "paused",
      pause: answered
        ? {}
        : { kind: "worker_request", message: question, automatic: false },
      worker_request: {
        id: requestId,
        kind: "verification",
        status: answered ? "answered" : "pending",
        question,
        context:
          "The project already defines the checks needed by the coding agent.",
        choices: [],
        role_id: "implementer",
        task_id: "implement",
        attempt: 1,
        ...(answered ? { response_source: "factory_verification" } : {}),
      },
      verification_handoff: {
        version: 1,
        id: handoffId,
        request_id: requestId,
        status: answered
          ? "completed"
          : state.phase === "held"
            ? "held"
            : "running",
        continuation: 1,
        max_continuations: 1,
        commands_count: 7,
        completed_commands: answered ? 7 : state.phase === "held" ? 2 : 0,
        failed_commands: state.phase === "held" ? 1 : 0,
        source_digest: "1".repeat(64),
        commands_digest: "2".repeat(64),
        policy_digest: "3".repeat(64),
        provider_digest: "4".repeat(64),
      },
    };
  }
  await page.context().route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== "GET") {
      state.mutations.push(`${request.method()} ${path}`);
      return route.fulfill({
        status: 405,
        json: { detail: "Read-only UI fixture" },
      });
    }
    if (path === `/api/v1/runs/${runId}/worker-messages`) {
      state.conversationReads++;
      return route.fulfill({
        json:
          state.phase === "answered" ? [feedback, factoryAnswer] : [feedback],
      });
    }
    if (path === `/api/v1/runs/${runId}`) {
      state.runReads.push(state.phase);
      return route.fulfill({ json: currentRun() });
    }
    return route.fulfill({
      json: path.endsWith("/repositories")
        ? [
            {
              id: repositoryId,
              name: "Verification fixture",
              url: "https://example.test/verification.git",
              provider: "git",
              default_branch: "main",
            },
          ]
        : path.endsWith("/runs")
          ? [currentRun()]
          : path.endsWith("/health")
            ? { status: "ok", execution_ready: false }
            : [],
    });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expectAuthenticated(page);
  return state;
}

async function openQuestions(page: Page) {
  await navigate(page, "Factory");
  await page
    .getByRole("button", { name: /Automatic verification UI fixture/ })
    .click();
  await page.getByRole("tab", { name: "Agent questions", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: question, exact: true }),
  ).toBeVisible();
  return page.getByRole("region", {
    name: "Agent questions and answers",
    exact: true,
  });
}

async function reachable(page: Page, control: Locator) {
  await control.scrollIntoViewIfNeeded();
  await control.focus();
  await expect(control).toBeFocused();
  await expect
    .poll(() =>
      control.evaluate((element) => {
        const rect = element.getBoundingClientRect();
        const header = document
          .querySelector(".workspace-main>header")
          ?.getBoundingClientRect();
        const footer = document
          .querySelector(".workspace-main>footer")
          ?.getBoundingClientRect();
        return (
          rect.width > 0 &&
          rect.height >= 40 &&
          rect.left >= 0 &&
          rect.right <= innerWidth + 1 &&
          rect.top >= Math.max(header?.bottom ?? 0, 0) - 1 &&
          rect.bottom <=
            Math.min(footer?.top ?? innerHeight, innerHeight) + 1 &&
          element.contains(
            document.elementFromPoint(
              rect.x + rect.width / 2,
              rect.y + rect.height / 2,
            ),
          )
        );
      }),
    )
    .toBe(true);
}

async function frame(
  page: Page,
  info: TestInfo,
  name: string,
  target: Locator,
) {
  await target.scrollIntoViewIfNeeded();
  await expect(target).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const selectors = [
          "html",
          "main",
          ".workspace-main",
          ".run-detail",
          ".worker-dialogue",
          ".worker-thread",
          ".verification-handoff",
          ".worker-recorded-answer",
        ];
        return selectors.flatMap((selector) =>
          [...document.querySelectorAll<HTMLElement>(selector)]
            .filter(
              (element) =>
                element.getBoundingClientRect().width > 0 &&
                element.scrollWidth > element.clientWidth + 1,
            )
            .map(() => selector),
        );
      }),
    )
    .toEqual([]);
  await info.attach(name, {
    body: await page.screenshot(),
    contentType: "image/png",
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
]) {
  for (const theme of ["light", "dark"] as const) {
    test(`automatic verification ownership, held review and factory attribution at ${viewport.width}x${viewport.height} ${theme}`, async ({
      page,
    }, info) => {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
      const state = await fixture(page);
      await page.getByLabel("Appearance", { exact: true }).selectOption(theme);
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      let dialogue = await openQuestions(page);
      const noManualAction = async () => {
        await expect(
          dialogue.getByText("Handled by forge", { exact: true }),
        ).toBeVisible();
        await expect(
          page.getByLabel("Your answer", { exact: true }),
        ).toHaveCount(0);
        for (const name of [
          "Review answer",
          "Send answer",
          "Resume run",
          "Hand off run",
        ])
          await expect(
            page.getByRole("button", { name, exact: true }),
          ).toHaveCount(0);
        expect(state.mutations).toEqual([]);
      };
      await noManualAction();
      await expect(
        dialogue.getByText("Running approved checks", { exact: true }),
      ).toBeVisible();
      await frame(
        page,
        info,
        "automatic-pending",
        dialogue.getByRole("heading", {
          name: "Automatic verification",
          exact: true,
        }),
      );

      state.phase = "reconciliation";
      await page.reload();
      dialogue = await openQuestions(page);
      await expect(
        dialogue.getByText("Running approved checks", { exact: true }),
      ).toBeVisible();
      await noManualAction();
      expect(state.runReads).toContain("reconciliation");
      await frame(
        page,
        info,
        "recovering-still-factory-owned",
        dialogue.getByRole("heading", {
          name: "Automatic verification",
          exact: true,
        }),
      );

      state.phase = "held";
      await page.reload();
      dialogue = await openQuestions(page);
      await expect(
        dialogue.getByText("Handled by forge", { exact: true }),
      ).toHaveCount(0);
      await expect(
        dialogue.getByText("Needs your decision", { exact: true }),
      ).toBeVisible();
      for (const name of ["Resume run", "Hand off run"])
        await expect(
          page.getByRole("button", { name, exact: true }),
        ).toHaveCount(0);
      const openQuestion = page
        .getByRole("region", { name: "Waiting for your answer", exact: true })
        .getByRole("button", { name: "Open agent questions", exact: true });
      await expect(
        page.getByRole("button", { name: "Open agent questions", exact: true }),
      ).toHaveCount(1);
      await reachable(page, openQuestion);
      const input = dialogue.getByLabel("Your answer", { exact: true });
      await reachable(page, input);
      await input.fill("Review the recorded failed check before continuing.");
      const review = dialogue.getByRole("button", {
        name: "Review answer",
        exact: true,
      });
      await reachable(page, review);
      await page.keyboard.press("Enter");
      const reviewHeading = dialogue.getByRole("heading", {
        name: "Review your answer",
        exact: true,
      });
      await expect(reviewHeading).toBeFocused();
      await frame(page, info, "held-human-review", reviewHeading);
      expect(state.mutations).toEqual([]);

      state.phase = "answered";
      await page.reload();
      dialogue = await openQuestions(page);
      const recorded = dialogue.getByRole("region", {
        name: "Recorded answer",
        exact: true,
      });
      await expect(recorded).toContainText(answerText);
      await expect(recorded).toContainText(
        "Recorded label: Forge verification.",
      );
      await expect(recorded).toContainText(
        "Handled by forge using the approved verification runner.",
      );
      await expect(recorded).not.toContainText("Test operator");
      await expect(recorded).not.toContainText("Authenticated native user");
      await expect(
        dialogue.getByLabel("Your answer", { exact: true }),
      ).toHaveCount(0);
      await frame(
        page,
        info,
        "factory-v4-recorded-answer",
        recorded.getByRole("heading", { name: "Recorded answer", exact: true }),
      );
      expect(state.conversationReads).toBeGreaterThanOrEqual(4);
      expect(state.mutations).toEqual([]);
      expect(
        await page.evaluate(() =>
          Object.keys(localStorage).filter((key) =>
            key.startsWith("forge.worker-reply.v1:"),
          ),
        ),
      ).toEqual([]);
    });
  }
}
