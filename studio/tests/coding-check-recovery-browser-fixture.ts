/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { expect, type Page } from "@playwright/test";
import fixture from "./coding-check-grant-fixture.json" with { type: "json" };
import { setup, prefix } from "./provider-recovery-allowance-browser-fixture";
import {
  candidate as allowance,
  context,
  receipt as finalReceipt,
} from "./provider-recovery-allowance-fixture";

// Controlled HTTP fixtures test presentation and request identity, not native authority or live QA.
export async function prepare(page: Page, mode = "success", existing = false) {
  await setup(page, "success", existing);
  const expires = Date.now() + 600000;
  const state = {
    grantPosts: 0,
    allowancePosts: 0,
    grantReads: 0,
    currentReads: 0,
    candidateReads: 0,
    granted: null as Record<string, unknown> | null,
    final: null as Record<string, unknown> | null,
    grantReadStatus: 200,
    currentStatus: "active",
    currentDigest: "4".repeat(64),
    release: () => {},
    grantCandidate: null as Record<string, unknown> | null,
  };
  await page.route("**/api/v1/runs/*/coding-check-grants**", async (route) => {
    const r = route.request(),
      url = new URL(r.url());
    if (url.pathname.endsWith("/candidate")) {
      state.candidateReads++;
      const c = structuredClone(fixture.candidate);
      c.run_id = context.run;
      c.agent = "codex";
      c.recovery_request_id = url.searchParams.get("recovery_request_id")!;
      c.channel.bind_port = Number(url.searchParams.get("bind_port"));
      state.grantCandidate = c;
      return route.fulfill({ json: c });
    }
    if (url.pathname.endsWith("/current")) {
      state.currentReads++;
      if (!state.granted) {
        if (mode === "current-denied")
          return route.fulfill({
            status: 403,
            json: { detail: "Private current-state denial" },
          });
        const status = mode.startsWith("current-") ? mode.slice(8) : "none";
        return route.fulfill({
          json:
            status === "none"
              ? {
                  protocol: fixture.current_active.protocol,
                  run_id: context.run,
                  grant_id: null,
                  revision: 0,
                  status,
                  receipt_digest: null,
                  expires_at_ms: null,
                }
              : {
                  ...fixture.current_active,
                  run_id: context.run,
                  status,
                  expires_at_ms: expires,
                },
        });
      }
      return route.fulfill({
        json: {
          ...fixture.current_active,
          run_id: context.run,
          grant_id: state.granted?.request_id,
          receipt_digest: state.currentDigest,
          status: state.currentStatus,
          expires_at_ms: expires,
        },
      });
    }
    if (r.method() === "POST") {
      state.grantPosts++;
      state.granted = r.postDataJSON();
      const values = await page.evaluate(
        (key) =>
          Object.entries(localStorage).filter(([k]) => k.startsWith(key)),
        prefix,
      );
      expect(values).toHaveLength(1);
      const marker = JSON.parse(values[0][1]);
      expect(marker.grant).toEqual(state.granted);
      expect(marker.allowance).toBeNull();
      if (mode === "grant-pending") {
        await new Promise<void>((resolve) => {
          state.release = resolve;
        });
        return route.abort("failed");
      }
      if (mode === "grant-lost") return route.abort("failed");
      return route.fulfill({
        status: 201,
        json: { ignored: "Must read exact receipt" },
      });
    }
    state.grantReads++;
    if (state.grantReadStatus !== 200 || !state.granted)
      return route.fulfill({
        status: state.grantReadStatus,
        json: { detail: "Private denial must not be displayed" },
      });
    return route.fulfill({
      json: {
        ...fixture.immutable_grant_receipt,
        request: state.granted,
        candidate: state.grantCandidate,
        actor: {
          ...fixture.immutable_grant_receipt.actor,
          user_id: mode === "wrong-actor" ? "0".repeat(32) : context.actor,
          authentication_method: context.authentication,
        },
        expires_at_ms: expires,
        receipt_digest: "4".repeat(64),
      },
    });
  });
  await page.route(
    "**/api/v1/runs/*/provider-recoveries/allowances**",
    async (route) => {
      const r = route.request(),
        url = new URL(r.url());
      if (url.pathname.endsWith("/candidate"))
        return route.fulfill({ json: allowance });
      if (r.method() === "POST") {
        state.allowancePosts++;
        state.final = r.postDataJSON();
        const markers = await page.evaluate(
          (key) =>
            Object.entries(localStorage).filter(([k]) => k.startsWith(key)),
          prefix,
        );
        expect(JSON.parse(markers[0][1]).allowance.body).toEqual(state.final);
        expect(state.final!.request_id).toBe(
          state.granted!.recovery_request_id,
        );
        expect(state.final!.coding_check_grant_id).toBe(
          state.granted!.request_id,
        );
        if (mode === "final-lost") return route.abort("failed");
        return route.fulfill({ status: 201, json: { ignored: "Read exact" } });
      }
      if (!state.final) return route.fulfill({ status: 404, json: {} });
      return route.fulfill({ json: await finalReceipt(state.final) });
    },
  );
  return state;
}
export async function open(page: Page) {
  await page
    .getByRole("button", {
      name: "Prepare recovery with configured checks",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review configured checks",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Trusted host execution", { exact: true }),
  ).toBeVisible();
}
export async function scope(page: Page) {
  await open(page);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Reason for granting check access", { exact: true })
    .fill("Reviewed these retained checks and the host runner controls.");
}
export async function grant(page: Page) {
  await scope(page);
  await page
    .getByRole("button", { name: "Grant selected checks", exact: true })
    .click();
}
export async function review(page: Page) {
  await grant(page);
  await expect(
    page.getByRole("heading", {
      name: "Review the recovery mitigation",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByLabel("Mitigation reviewed", { exact: true })
    .fill("Reviewed the fixed provider and native process evidence.");
  await page
    .getByLabel("Mitigation evidence SHA-256", { exact: true })
    .fill("e".repeat(64));
  await page
    .getByLabel("Adopt the reviewed current CLI for this final recovery.", {
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Review final recovery", exact: true })
    .click();
  await page.getByLabel(/I understand this spends/).check();
}
