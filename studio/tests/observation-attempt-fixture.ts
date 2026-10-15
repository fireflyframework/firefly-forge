/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { createHash } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { navigate } from "./navigation";
import { record } from "./learning-record-fixture";
export const actor = "9".repeat(32),
  source = "owned-procedure-source";
export const projection = {
  id: "1".repeat(32),
  record_id: record.id,
  state: "source_admitted",
  request_id: "2".repeat(32),
  workspace_id: "forge-learning-owned-inventory",
  source_id: source,
  source_status: "queued",
  payload_digest: "3".repeat(64),
  content_sha256: "4".repeat(64),
  target: { revision: "5".repeat(32) },
  citations: [],
};
export async function fixture(
  page: Page,
  options: {
    lost?: boolean;
    terminal?: boolean;
    receipt?: string;
    attemptState?: "preparing" | "dispatched" | "unknown";
    changedProjection?: boolean;
    changedReceipt?: boolean;
    changedTerminalState?: boolean;
    unassignedSource?: boolean;
    viewer?: boolean;
    storage?: boolean;
    projectionState?: string;
    holdReceipt?: boolean;
    denyProjectionAfterReceipt?: boolean;
    currentRecordState?: "verified" | "invalidated";
  } = {},
) {
  let releaseReceipt = () => {};
  const heldReceipt = new Promise<void>((done) => {
    releaseReceipt = done;
  });
  let denialStatus = 403;
  let writes = 0,
    reads = 0,
    denied = false,
    currentActor = actor,
    body: {
      attempt_id: string;
      source_id: string;
      projection_id: string;
      submission_request_id: string;
      workspace_id: string;
      target_revision: string;
      payload_digest: string;
      content_sha256: string;
    } | null = null;
  const bodies: unknown[] = [];
  const dispatchMarkers: Record<string, unknown>[] = [];
  let attemptReads = 0;
  if (options.storage)
    await page.addInitScript(() => {
      const set = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.startsWith("forge.learning-observation"))
          throw new Error("Storage unavailable");
        return set.call(this, key, value);
      };
    });
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      path = new URL(req.url()).pathname;
    if (path.endsWith("/auth/configuration"))
      return route.fulfill({ json: { mode: "local_bootstrap" } });
    if (path.endsWith("/auth/session"))
      return route.fulfill({
        json: {
          authenticated: true,
          user: {
            id: currentActor,
            display_name: "Owned reviewer",
            email: null,
            authentication_method: "local_bootstrap",
          },
          global_admin: false,
          memberships: [
            {
              project_id: record.envelope.context.project_id,
              role: options.viewer ? "viewer" : "reviewer",
              revision: 1,
              permissions: options.viewer ? ["read"] : ["read", "review"],
            },
          ],
          roles: {},
        },
      });
    if (path.endsWith("/repositories"))
      return route.fulfill({
        json: [
          {
            id: record.envelope.context.repository_id,
            name: "Owned inventory",
            url: "/owned/inventory",
            provider: "local",
            default_branch: "main",
          },
        ],
      });
    if (path.endsWith("/learning") && path.includes("/repositories/"))
      return route.fulfill({
        json: [
          {
            ...record,
            state: writes
              ? (options.currentRecordState ?? "verified")
              : "verified",
          },
        ],
      });
    if (
      path.endsWith("/projection") &&
      options.denyProjectionAfterReceipt &&
      reads
    )
      return route.fulfill({
        status: 403,
        json: { detail: "Projection read denied" },
      });
    if (path.endsWith("/projection"))
      return route.fulfill({
        json: {
          ...projection,
          ...(options.unassignedSource ? { source_id: null } : {}),
          source_status:
            writes && !options.terminal ? "ingested" : projection.source_status,
          ...(options.changedProjection && attemptReads > 1
            ? { id: "8".repeat(32) }
            : {}),
          state:
            writes && !options.terminal
              ? "verified"
              : (options.projectionState ?? "source_admitted"),
          citations:
            (writes && !options.terminal) ||
            options.projectionState === "verified"
              ? [{ source_id: source, chunk_id: "owned-chunk:1" }]
              : [],
        },
      });
    if (path.endsWith("/projection/observation-attempts")) {
      writes++;
      body = req.postDataJSON();
      bodies.push(body);
      const retained = await page.evaluate(() => {
        const key = Object.keys(sessionStorage).find(
          (key) =>
            key.startsWith("forge.learning-observation.v2:") &&
            !key.includes(":history:"),
        );
        return key ? JSON.parse(sessionStorage.getItem(key)!) : null;
      });
      expect(retained?.requestId).toBe(body!.attempt_id.slice(5));
      expect(retained?.sourceDigest).toBe(hash({ source_id: body!.source_id }));
      expect(retained?.inputDigest).toBe(hash(body));
      expect(retained).not.toHaveProperty("source");
      dispatchMarkers.push(retained);
      if (options.lost) return route.abort("failed");
      return route.fulfill({ json: {} });
    }
    if (path.includes("/projection/observation-attempts/")) {
      attemptReads++;
      if (options.holdReceipt) await heldReceipt;
      if (denied)
        return route.fulfill({
          status: denialStatus,
          json: { detail: "Review authority revoked" },
        });
      if (options.receipt === "absent" || !body)
        return route.fulfill({
          status: 404,
          json: { detail: "Exact attempt not observed" },
        });
      const packet = {
        version: 2,
        attempt_id: body.attempt_id,
        record_id: record.id,
        repository_id: record.envelope.context.repository_id,
        project_id: record.envelope.context.project_id,
        run_id: record.envelope.run_id,
        actor_id: actor,
        input_digest: hash(body),
        source_digest: hash({ source_id: body.source_id }),
        projection_id: body.projection_id,
        submission_request_id: body.submission_request_id,
        workspace_id: body.workspace_id,
        target_revision: body.target_revision,
        payload_digest: body.payload_digest,
        content_sha256: body.content_sha256,
        state:
          options.attemptState ??
          (options.terminal ? "not_dispatched" : "verified"),
        disposition: options.attemptState
          ? null
          : options.terminal
            ? "local_admission_refused"
            : "verified",
        created_at: "2026-10-15T06:00:00Z",
        finished_at:
          options.attemptState && options.attemptState !== "unknown"
            ? null
            : "2026-10-15T06:00:01Z",
        observation:
          options.terminal || options.attemptState
            ? null
            : {
                event_id: "6".repeat(32),
                request_id: body.attempt_id.slice(5),
                digest: "7".repeat(64),
              },
      };
      if (options.receipt === "request")
        packet.attempt_id = "obs2_" + "8".repeat(32);
      if (options.receipt === "actor") packet.actor_id = "8".repeat(32);
      if (options.receipt === "source") packet.source_digest = "8".repeat(64);
      if (options.receipt === "target") packet.target_revision = "8".repeat(32);
      if (options.changedReceipt && attemptReads > 1)
        packet.finished_at = "2026-10-15T06:00:02Z";
      if (options.changedTerminalState && attemptReads > 1) {
        packet.state = "verified";
        packet.disposition = "verified";
        packet.observation = {
          event_id: "6".repeat(32),
          request_id: body.attempt_id.slice(5),
          digest: "7".repeat(64),
        };
      }
      const receipt = { ...packet, digest: hash(packet) };
      if (options.receipt === "tampered")
        receipt.finished_at = "2026-10-15T06:00:02Z";
      return route.fulfill({ json: receipt });
    }
    if (path.includes("/projection/observations/")) {
      reads++;
      if (options.holdReceipt) await heldReceipt;
      if (denied)
        return route.fulfill({
          status: denialStatus,
          json: { detail: "Review authority revoked" },
        });
      if (options.receipt === "absent")
        return route.fulfill({
          status: 404,
          json: { detail: "Exact observation request not observed" },
        });
      const receipt = {
        event_id: "6".repeat(32),
        record_id: record.id,
        repository_id: record.envelope.context.repository_id,
        run_id: record.envelope.run_id,
        request_id: body!.attempt_id.slice(5),
        kind: "projection_observation",
        event_state: "verified",
        current_record_state: options.currentRecordState ?? "verified",
        current_projection_state: "verified",
        actor_id: actor,
        projection_id: projection.id,
        submission_request_id: projection.request_id,
        workspace_id: projection.workspace_id,
        source_id: body!.source_id,
        target_revision: projection.target.revision,
        payload_digest: projection.payload_digest,
        content_sha256: projection.content_sha256,
        citations: [{ source_id: body!.source_id, chunk_id: "owned-chunk:1" }],
        created_at: "2026-10-15T06:00:00Z",
        sequence: 4,
        digest: "7".repeat(64),
      };
      if (options.receipt === "event") receipt.event_id = "8".repeat(32);
      if (options.receipt === "eventDigest") receipt.digest = "8".repeat(64);
      if (options.receipt === "request") receipt.request_id = "8".repeat(32);
      if (options.receipt === "source") receipt.source_id = "different-source";
      if (options.receipt === "actor") receipt.actor_id = "8".repeat(32);
      if (options.receipt === "target")
        receipt.target_revision = "8".repeat(32);
      return route.fulfill({
        json:
          options.receipt === "legacy"
            ? { ...receipt, actor_id: null }
            : receipt,
      });
    }
    return route.fulfill({
      json: [
        "/runs",
        "/projects",
        "/providers",
        "/deployment-targets",
        "/assistant/conversations",
      ].some((s) => path.endsWith(s))
        ? []
        : {},
    });
  });
  return {
    writes: () => writes,
    reads: () => attemptReads,
    eventReads: () => reads,
    attemptReads: () => attemptReads,
    bodies: () => bodies,
    dispatchMarkers: () => dispatchMarkers,
    body: () => body,
    deny: (status = 403) => {
      denied = true;
      denialStatus = status;
    },
    releaseReceipt: () => releaseReceipt(),
    changeActor: () => {
      currentActor = "8".repeat(32);
    },
  };
}
export async function select(page: Page, reload = true) {
  if (reload) await page.goto("/");
  await navigate(page, "Knowledge");
  await page
    .getByRole("tab", { name: "Learning records", exact: true })
    .click();
  await page
    .getByRole("button", { name: record.envelope.question, exact: true })
    .click();
}
export async function review(page: Page) {
  await page
    .getByRole("button", { name: "Verify existing source", exact: true })
    .click();
  await page
    .getByLabel("Existing source identifier", { exact: true })
    .fill(source);
  await page
    .getByRole("button", { name: "Review source", exact: true })
    .click();
  await expect(
    page.getByRole("button", {
      name: "Verify source",
      exact: true,
    }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I understand this reads the existing source and records a local verification; it does not submit or ingest a source.",
      { exact: true },
    )
    .check();
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  const row = value as Record<string, unknown>;
  return (
    "{" +
    Object.keys(row)
      .sort()
      .map((key) => JSON.stringify(key) + ":" + canonical(row[key]))
      .join(",") +
    "}"
  );
}
function hash(value: unknown) {
  return createHash("sha256").update(canonical(value)).digest("hex");
}
