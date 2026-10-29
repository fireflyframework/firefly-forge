/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, test } from "@playwright/test";
import { navigate } from "./navigation";
import { mockIdentity } from "./auth-fixture";
import { retainedRunCreation } from "./run-creation-cancellation-fixture";
test("verified run exposes diff, checks, artifact download and explicit publication", async ({
  page,
}) => {
  const run = {
    id: "verified-1",
    repository_id: "repo-1",
    brief: "Reservation API",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "succeeded",
    changed_files: ["src/reservations.py"],
    diff: "diff --git a/src/reservations.py b/src/reservations.py\n+def reserve():\n+    return True",
    verification: [
      {
        argv: ["make", "check"],
        exit_code: 0,
        stdout: "Behavior tests passed",
      },
    ],
    artifacts: [{ name: "changes.zip", size: 20 }],
    pr_url: "",
  };
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/artifacts/changes.zip")) {
      await route.fulfill({
        contentType: "application/zip",
        body: Buffer.from("PK-test-artifact"),
      });
      return;
    }
    if (path.endsWith("/publish")) {
      expect(route.request().method()).toBe("POST");
      run.pr_url = "https://github.com/example/rooms/pull/1";
    }
    const body = path.endsWith("/repositories")
      ? [
          {
            id: "repo-1",
            name: "Rooms",
            url: "/owned/source",
            provider: "github",
            default_branch: "main",
          },
        ]
      : path.endsWith("/runs")
        ? [run]
        : path.endsWith("/providers")
          ? []
          : path.endsWith("/profiles")
            ? {}
            : run;
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("entered-operator-token-for-test");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(
    page.getByRole("button", { name: "API connected" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Factory", exact: true }).click();
  await page.getByRole("button", { name: /Reservation API/ }).click();
  await expect(
    page.getByRole("button", { name: "Retry run" }),
  ).not.toBeVisible();
  await page.getByRole("tab", { name: "Changes" }).click();
  await expect(page.getByLabel("Git diff")).toContainText("+def reserve():");
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "Verification" })).toBeFocused();
  await expect(
    page.getByText("Behavior tests passed", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Artifacts" }).click();
  const downloaded = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download changes" }).click();
  expect((await downloaded).suggestedFilename()).toBe(
    "forge-verified-1-changes.zip",
  );
  await page.getByRole("button", { name: "Publish pull request" }).click();
  await expect(
    page.getByRole("link", { name: "Open pull request" }),
  ).toHaveAttribute("href", run.pr_url);
});
test("connect, register a generic repository, create a local run, cancel and retry", async ({
  page,
}) => {
  const repository = {
    id: "a".repeat(32),
    name: "Meeting rooms",
    url: "/owned/source",
    provider: "git",
    default_branch: "main",
  };
  let registered = false;
  const run = {
    id: "b".repeat(32),
    repository_id: repository.id,
    brief: "Build a meeting-room service",
    backend: "pyfly",
    frontend: "none",
    agent: "codex",
    status: "developing",
    events: [
      {
        stage: "developing",
        message: "Provider started",
        at: "2026-10-11T12:00:00Z",
      },
    ],
  };
  let submitted: Record<string, unknown> | null = null;
  const creation = retainedRunCreation(run.id);
  await page.route("**/api/v1/**", async (route) => {
    if (await mockIdentity(route)) return;
    expect(route.request().headers().authorization).toBeUndefined();
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path.endsWith("/repositories")) {
      if (route.request().method() === "POST") {
        expect(route.request().postDataJSON()).toEqual({
          name: "Meeting rooms",
          url: "/owned/source",
          provider: "git",
          default_branch: "main",
        });
        registered = true;
        body = repository;
      } else body = registered ? [repository] : [];
    } else if (path.endsWith("/providers"))
      body = [
        {
          id: "codex",
          name: "Codex",
          ready: true,
          detail: "Official CLI signed in",
        },
      ];
    else if (path.endsWith("/profiles"))
      body = {
        backends: [
          { id: "pyfly", name: "PyFly", validation: "Upstream inspected" },
        ],
        frontends: [],
      };
    else if (/\/run-creation-requests\/[a-f0-9]{32}$/.test(path)) {
      expect(route.request().method()).toBe("POST");
      body = await creation.admit(path, route.request().postDataJSON());
      submitted = creation.input;
    } else if (path.endsWith("/runs")) {
      expect(route.request().method()).toBe("GET");
      body = submitted ? [run] : [];
    } else if (path.endsWith("/cancel")) {
      run.status = "cancelled";
      body = run;
    } else if (path.endsWith("/retry")) {
      run.status = "queued";
      body = { ...run, id: "c".repeat(32) };
    } else body = run;
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("entered-operator-token-for-test");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(
    page.getByRole("button", { name: "API connected" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Repositories", exact: false })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Add repository", exact: true })
    .first()
    .click();
  await expect(
    page.getByLabel("Repository name", { exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Add repository", exact: true }).first(),
  ).toBeFocused();
  await page
    .getByRole("button", { name: "Add repository", exact: true })
    .first()
    .click();
  await page
    .getByLabel("Repository name", { exact: true })
    .fill("Meeting rooms");
  await page.getByLabel("Git URL", { exact: true }).fill("/owned/source");
  await page.getByLabel("Provider", { exact: true }).selectOption("git");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Add repository" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Meeting rooms" }),
  ).toBeVisible();
  await navigate(page, "Start a run");
  await page.getByLabel("What should this change accomplish?").fill(run.brief);
  await page
    .getByLabel("Source repository", { exact: true })
    .selectOption(repository.id);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Frontend", { exact: true }).selectOption("none");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Commands to run", { exact: true })
    .fill('[["make","check"]]');
  await page.getByRole("button", { name: "Review run", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Review your run" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Start building" }).click();
  await expect(page.getByRole("heading", { name: run.brief })).toBeVisible();
  expect(submitted).toMatchObject({
    publish: false,
    repository_id: repository.id,
    backend: "pyfly",
    frontend: "none",
    commands: [{ argv: ["make", "check"], timeout: null }],
  });
  await expect(
    page.getByText("Provider started", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel run" }).click();
  await expect(page.getByRole("button", { name: "Retry run" })).toBeVisible();
  await page.getByRole("button", { name: "Retry run" }).click();
  await expect(
    page.getByText(`Run ${"c".repeat(32)}`, { exact: true }),
  ).toBeVisible();
});
test("phone navigation, empty form and connection error remain usable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/**", async (route) => {
    if (new URL(route.request().url()).pathname.endsWith("/auth/login"))
      return route.fulfill({ status: 401, json: { detail: "Token rejected" } });
    if (await mockIdentity(route)) return;
    await route.fulfill({ status: 401, json: { detail: "Sign in required" } });
  });
  await page.goto("/");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await navigate(page, "Start a run");
  await expect(
    page.getByRole("button", { name: "Continue", exact: true }),
  ).toBeDisabled();
  await navigate(page, "Connections");
  await page
    .getByLabel("API token", { exact: true })
    .fill("invalid-operator-token-for-test");
  await page.getByRole("button", { name: "Save connection" }).click();
  await expect(page.getByRole("alert")).toContainText("Token rejected");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
