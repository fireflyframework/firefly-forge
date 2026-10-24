/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import gitDelivery from "./git-delivery-contract-fixture.json" with { type: "json" };
import native from "./campaign-report-archive-fixture.json" with { type: "json" };
const verifiedReport = native.receipt.report;
// These controlled API fixtures qualify read presentation, not native campaign execution.
const projectId = verifiedReport.project_id,
  campaignId = verifiedReport.campaign.id,
  repositoryId = verifiedReport.components[0].repository_id;
const component = {
  id: "api",
  name: "Inventory API",
  repository_id: repositoryId,
  path: ".",
  responsibility: "Fictional inventory service",
  kind: "api",
  backend: "pyfly",
  frontend: "none",
  verification: [],
  depends_on: [],
};
const project = {
  id: projectId,
  revision: 1,
  digest: "a".repeat(64),
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  definition: {
    name: "Owned campaign product",
    description: "Fictional local receipt inspection",
    owners: ["Owned fixture"],
    topology: "monorepo",
    product_repository_id: repositoryId,
    components: [component],
  },
};
function canonical(value: unknown): string {
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  if (value && typeof value === "object")
    return (
      "{" +
      Object.entries(value)
        .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
        .map(([k, v]) => JSON.stringify(k) + ":" + canonical(v))
        .join(",") +
      "}"
    );
  return JSON.stringify(value);
}
function digest(value: unknown) {
  return createHash("sha256").update(canonical(value)).digest("hex");
}
async function setup(page: Page, readOnly = false, apiRoot = false) {
  const state = {
    lists: 0,
    reports: 0,
    writes: 0,
    campaigns: [] as unknown[],
    listStatus: 200,
    reportStatus: 200,
    report: verifiedReport as unknown,
    archiveLists: 0,
    archiveReads: 0,
    archivePosts: 0,
    archiveReadStatus: 200,
    archivePageStatus: 200,
    archiveReceipt: structuredClone(native.receipt) as unknown,
    wrongReceiptActor: false,
    archiveNextPage: null as unknown,
    archivePage: structuredClone(native.page) as unknown,
    postMode: "unknown" as "unknown" | "success" | "lost",
    postBodies: [] as { request_id: string; expected_report_digest: string }[],
    markerBeforePost: false,
    archiveGate: null as Promise<void> | null,
    reportGate: null as Promise<void> | null,
  };
  await page.route(
    (url) =>
      url.pathname.startsWith("/api/v1/") ||
      (apiRoot &&
        /^\/(auth\/|projects(?:\/|$)|repositories(?:\/|$)|runs(?:\/|$)|providers(?:\/|$)|profiles(?:\/|$)|health$)/.test(
          url.pathname,
        )),
    async (route) => {
      if (
        readOnly &&
        new URL(route.request().url()).pathname.endsWith("/auth/session") &&
        route.request().headers()["cookie"]?.includes("forge_session")
      )
        return route.fulfill({
          json: {
            authenticated: true,
            user: {
              id: "f".repeat(32),
              display_name: "Project reader",
              email: null,
              authentication_method: "local_bootstrap",
            },
            global_admin: false,
            memberships: [
              { project_id: projectId, role: "viewer", permissions: ["read"] },
            ],
            roles: { viewer: ["read"] },
          },
        });
      if (await mockIdentity(route)) return;
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith(`/projects/${projectId}/git-delivery-policy`)) {
        if (route.request().method() !== "GET")
          return route.fulfill({
            status: 405,
            json: { detail: "This fixture exposes policy reads only" },
          });
        return route.fulfill({
          json: {
            ...gitDelivery.policy_default,
            project_id: projectId,
            permissions: { configure: !readOnly, publish: !readOnly },
            remote_capabilities: [
              {
                repository_id: repositoryId,
                provider: "git",
                draft_updates: false,
                reason: "This provider supports local checkpoints only.",
              },
            ],
          },
        });
      }
      if (path.includes("/report-archives")) {
        if (route.request().method() === "POST") {
          state.archivePosts++;
          const body = route.request().postDataJSON() as {
            request_id: string;
            expected_report_digest: string;
          };
          state.postBodies.push(body);
          state.markerBeforePost = await page.evaluate(
            (body) =>
              Object.entries(localStorage).some(([key, raw]) => {
                if (!key.startsWith("forge.campaign-report-archive.v1:"))
                  return false;
                const m = JSON.parse(raw);
                return (
                  m.requestId === body.request_id &&
                  m.reportDigest === body.expected_report_digest
                );
              }),
            body,
          );
          const receipt = {
            ...structuredClone(native.receipt),
            request_id: body.request_id,
            actor_id: "f".repeat(32),
            request_digest: digest(body),
            digest: "",
          };
          if (state.wrongReceiptActor) receipt.actor_id = "a".repeat(32);
          receipt.digest = digest(
            Object.fromEntries(
              Object.entries(receipt).filter(([key]) => key !== "digest"),
            ),
          );
          state.archiveReceipt = receipt;
          if (state.postMode === "lost") return route.abort("failed");
          return route.fulfill({
            status: state.postMode === "success" ? 200 : 503,
            json:
              state.postMode === "success"
                ? receipt
                : { detail: "private delivery canary" },
          });
        }
        if (path.endsWith("/report-archives")) {
          state.archiveLists++;
          return route.fulfill({
            status: state.archivePageStatus,
            json:
              new URL(route.request().url()).searchParams.get("after") === "50"
                ? state.archiveNextPage
                : state.archivePage,
          });
        }
        state.archiveReads++;
        const value = structuredClone(state.archiveReceipt);
        if (state.archiveGate) await state.archiveGate;
        return route.fulfill({ status: state.archiveReadStatus, json: value });
      }
      if (path.includes("/campaigns")) {
        if (route.request().method() !== "GET") state.writes++;
        if (path.endsWith("/report")) {
          state.reports++;
          const report = state.report;
          if (state.reportGate) await state.reportGate;
          return route.fulfill({
            status: state.reportStatus,
            json: report ?? { detail: "private report error canary" },
          });
        }
        state.lists++;
        return route.fulfill({
          status: state.listStatus,
          json:
            state.listStatus === 200
              ? state.campaigns
              : { detail: "private list error canary" },
        });
      }
      const json = path.endsWith("/repositories")
        ? [
            {
              id: repositoryId,
              name: "Owned fixture",
              url: "/fixture",
              provider: "git",
              default_branch: "main",
            },
          ]
        : path.endsWith("/projects")
          ? [project]
          : path.endsWith("/projects/" + projectId)
            ? {
                ...project,
                components: [
                  {
                    ...component,
                    latest_status: "not_started",
                    repository_run_count: 0,
                    runs: [],
                  },
                ],
                counts: { not_started: 1 },
              }
            : path.endsWith("/health")
              ? {
                  status: "ok",
                  service_role: "api",
                  admission_ready: true,
                  execution_ready: false,
                  detail: null,
                  execution: {
                    local_enabled: false,
                    remote_worker_state: "unknown",
                  },
                }
              : [];
      await route.fulfill({ json });
    },
  );
  await page.goto("/");
  await navigate(page, "Connections");
  if (apiRoot) {
    await page
      .getByLabel("API address", { exact: true })
      .fill(new URL(page.url()).origin);
    await page
      .getByLabel("API token", { exact: true })
      .fill("owned-fixture-at-least-24-characters");
    await page
      .getByRole("button", { name: "Load server", exact: true })
      .click();
  }
  await page
    .getByLabel("API token", { exact: true })
    .fill("owned-fixture-at-least-24-characters");
  await page
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "API connected", exact: true }),
  ).toBeVisible();
  await navigate(page, "Product projects");
  await expect(
    page.getByRole("heading", { name: "Owned campaign product", exact: true }),
  ).toBeVisible();
  return state;
}
async function selectCampaign(
  page: Page,
  state: Awaited<ReturnType<typeof setup>>,
) {
  state.campaigns = [
    {
      id: campaignId,
      project_id: projectId,
      status: "running",
      created_at: "2026-10-15T00:00:00Z",
    },
  ];
  await page
    .getByRole("button", { name: "Load campaigns", exact: true })
    .click();
  await page.getByLabel("Campaign", { exact: true }).selectOption(campaignId);
}
async function reviewSave(page: Page) {
  await page.getByRole("button", { name: "View report", exact: true }).click();
  await page
    .getByRole("button", { name: "Save this report", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Save campaign report",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "Review save", exact: true })
    .click();
  await dialog
    .getByLabel("I understand that saving does not approve or run work.", {
      exact: true,
    })
    .check();
  return dialog;
}

export {
  setup,
  selectCampaign,
  reviewSave,
  native,
  verifiedReport,
  projectId,
  campaignId,
  digest,
};
