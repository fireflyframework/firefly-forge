/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import packet from "./scm-delegation-contract-fixture.json" with { type: "json" };
import { mockIdentity } from "./auth-fixture";
import { navigate } from "./navigation";
import type {
  DelegationHead,
  DelegationInput,
  DelegationReceipt,
} from "../src/ScmDelegationData";

// Constructed guide/test responses follow the source-bound Python models; no provider is contacted.
const sorted = (value: unknown): unknown =>
  Array.isArray(value)
    ? value.map(sorted)
    : value && typeof value === "object"
      ? Object.fromEntries(
          Object.entries(value)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([key, entry]) => [key, sorted(entry)]),
        )
      : value;
export const digest = (value: unknown) =>
  createHash("sha256")
    .update(JSON.stringify(sorted(value)))
    .digest("hex");
export const projectId = packet.receipt.project_id;
export const repositoryId = packet.receipt.repository_id;
export const nativeUser = packet.request.native_user_id;
const definition = {
  name: "Inventory product",
  description: "Fictional delegation fixture",
  owners: ["Fixture owner"],
  topology: "monorepo",
  roles: null,
  product_repository_id: repositoryId,
  components: [
    {
      id: "api",
      name: "Inventory API",
      repository_id: repositoryId,
      path: ".",
      kind: "api",
      backend: "pyfly",
      frontend: "none",
      verification: [],
      depends_on: [],
      responsibility: "Inventory requests",
    },
  ],
};
export const project = {
  id: projectId,
  revision: 1,
  digest: digest(definition),
  created_at: "2026-10-15T00:00:00Z",
  updated_at: "2026-10-15T00:00:00Z",
  definition,
};
export const projectCut = digest(project);
export function receiptFor(
  body: DelegationInput,
  actor = "f".repeat(32),
  revision = 1,
): DelegationReceipt {
  const value = {
    ...packet.receipt,
    event_id: revision.toString(16).padStart(32, "0"),
    project_binding_digest: body.expected_project_binding_digest,
    revision,
    previous_digest: body.expected_previous_digest,
    administering_user_id: actor,
    request: body,
    request_digest: digest(body),
  };
  const unsigned = Object.fromEntries(
    Object.entries(value).filter(([key]) => key !== "digest"),
  );
  return { ...unsigned, digest: digest(unsigned) } as DelegationReceipt;
}
export function headFor(
  receipt: DelegationReceipt,
  headStatus: DelegationHead["head_status"] = "active",
): DelegationHead {
  const base = {
    version: 1 as const,
    event_id: receipt.event_id,
    project_id: projectId,
    repository_id: repositoryId,
    project_binding_digest: receipt.project_binding_digest,
    request_id: receipt.request.request_id,
    binding_key: receipt.binding_key,
    revision: receipt.revision,
    digest: receipt.digest,
    native_user_id: receipt.request.native_user_id,
    component_ids: receipt.request.component_ids,
    allowed_actions: receipt.request.allowed_actions,
    active: receipt.request.active,
    provider: receipt.verified_reference.provider,
    external_actor_id: receipt.verified_reference.external_actor_id,
    prepared_at: receipt.prepared_at,
  };
  return {
    ...base,
    summary_digest: digest(base),
    current_head: true,
    head_status: headStatus,
  };
}
export async function setupDelegation(
  page: Page,
  options: {
    reader?: boolean;
    existingSession?: boolean;
    heads?: DelegationHead[];
    initialReceipt?: DelegationReceipt;
    cut?: string;
    empty?: boolean;
  } = {},
) {
  const state = {
    posts: [] as DelegationInput[],
    exactReads: [] as string[],
    markersAtPost: [] as unknown[],
    writes: [] as string[],
    receipts: new Map<string, DelegationReceipt>(),
    exactStatus: 200,
    postStatus: 201,
    loseResponse: false,
    publishReceipt: true,
    candidatesCut: options.cut ?? projectCut,
    heads: options.heads ?? [],
    actor: "f".repeat(32),
    eligibleGate: null as Promise<void> | null,
    exactGate: null as Promise<void> | null,
  };
  if (options.initialReceipt)
    state.receipts.set(
      options.initialReceipt.request.request_id,
      options.initialReceipt,
    );
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      path = url.pathname;
    if (
      path.endsWith("/auth/session") &&
      req.headers().cookie?.includes("forge_session")
    )
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: state.actor,
            display_name: options.reader ? "Project reader" : "Test operator",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: !options.reader,
          memberships: options.reader
            ? [{ project_id: projectId, role: "viewer", permissions: ["read"] }]
            : [],
          roles: { viewer: ["read"] },
        },
      });
    if (await mockIdentity(route)) return;
    if (path.includes("scm-actor-bindings")) {
      if (req.method() === "POST") {
        const body = req.postDataJSON() as DelegationInput;
        state.posts.push(body);
        state.markersAtPost.push(
          await page.evaluate(() =>
            Object.entries(localStorage)
              .filter(([key]) => key.startsWith("forge.scm-delegation.v1:"))
              .map(([, value]) => JSON.parse(value)),
          ),
        );
        if (state.publishReceipt)
          state.receipts.set(
            body.request_id,
            receiptFor(
              body,
              state.actor,
              body.expected_previous_digest
                ? [...state.receipts.values()].find(
                    (item) => item.digest === body.expected_previous_digest,
                  )!.revision + 1
                : 1,
            ),
          );
        if (state.loseResponse) return route.abort("failed");
        return route.fulfill({
          status: state.postStatus,
          json:
            state.postStatus === 201
              ? state.receipts.get(body.request_id)
              : { detail: "The reviewed project cut changed." },
        });
      }
      if (path.includes("/requests/")) {
        const id = path.split("/").at(-1)!;
        state.exactReads.push(id);
        if (state.exactGate) await state.exactGate;
        const receipt = state.receipts.get(id);
        return route.fulfill({
          status:
            state.exactStatus !== 200 ? state.exactStatus : receipt ? 200 : 404,
          json:
            state.exactStatus === 200 && receipt
              ? receipt
              : { detail: "Original receipt is not available." },
        });
      }
      if (path.endsWith("/candidates"))
        return route.fulfill({
          json: {
            ...packet.candidates,
            project_binding_digest: state.candidatesCut,
            items: options.empty ? [] : packet.candidates.items,
          },
        });
      return route.fulfill({
        json: {
          version: 1,
          project_id: projectId,
          repository_id: repositoryId,
          project_binding_digest: projectCut,
          heads_only: true,
          items: state.heads,
          next_cursor: null,
        },
      });
    }
    if (path.includes("/eligible-users")) {
      if (state.eligibleGate) await state.eligibleGate;
      return route.fulfill({
        json: [
          {
            id: nativeUser,
            display_name: "Warehouse reviewer",
            email: null,
            contact_email: null,
            enabled: true,
          },
        ],
      });
    }
    if (req.method() !== "GET") state.writes.push(path);
    return route.fulfill({
      json: path.endsWith("/projects")
        ? [project]
        : path.endsWith("/repositories")
          ? [
              {
                id: repositoryId,
                name: "Inventory repository",
                url: "https://github.example.test/fixture/inventory",
              },
            ]
          : path.endsWith("/health")
            ? {
                status: "ok",
                service_role: "api",
                admission_ready: true,
                execution_ready: false,
                detail: "",
                execution: { local_enabled: false },
              }
            : path.endsWith("/profiles")
              ? {}
              : [],
    });
  });
  await page.goto("/", { waitUntil: "domcontentloaded" });
  await navigate(page, "Connections");
  if (options.existingSession) {
    await expect(
      page.getByRole("button", {
        name: `Signed in as ${options.reader ? "Project reader" : "Test operator"}`,
        exact: true,
      }),
    ).toBeVisible();
    await expect(page.getByLabel("API token", { exact: true })).toHaveCount(0);
  } else {
    await page
      .getByLabel("API token", { exact: true })
      .fill("fictional-scm-bootstrap-token-at-least24");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
  }
  await navigate(page, "Project access");
  return state;
}
export async function delegationReview(
  page: Page,
  capture?: (stage: "account" | "scope" | "review") => Promise<void>,
) {
  const panel = page.getByRole("region", {
    name: "Source-control delegation",
    exact: true,
  });
  await panel
    .getByRole("button", { name: "Review a delegation", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Review SCM delegation",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await dialog
    .getByLabel("Webhook-reported account", { exact: true })
    .selectOption(packet.request.verified_receipt_id);
  await capture?.("account");
  await dialog
    .getByRole("button", { name: "Continue to scope", exact: true })
    .click();
  await dialog
    .getByLabel("Find a registered person", { exact: true })
    .fill("Warehouse");
  await dialog
    .getByRole("button", { name: "Find person", exact: true })
    .click();
  await expect(
    dialog.getByLabel("Registered person", { exact: true }),
  ).toContainText("Warehouse reviewer");
  await dialog
    .getByLabel("Registered person", { exact: true })
    .selectOption(nativeUser);
  await dialog
    .getByRole("checkbox", { name: "Inventory API", exact: true })
    .check();
  await dialog
    .getByRole("checkbox", { name: "Answer agent questions", exact: true })
    .check();
  await capture?.("scope");
  await dialog
    .getByRole("button", { name: "Review delegation", exact: true })
    .click();
  await expect(
    dialog.getByRole("heading", { name: "Confirm delegation", exact: true }),
  ).toBeVisible();
  await capture?.("review");
  return dialog;
}
export async function saveDelegation(page: Page) {
  const dialog = await delegationReview(page);
  await dialog
    .getByRole("checkbox", {
      name: "I trust this webhook assertion.",
      exact: true,
    })
    .check();
  await dialog
    .getByRole("button", { name: "Save delegation", exact: true })
    .click();
}
