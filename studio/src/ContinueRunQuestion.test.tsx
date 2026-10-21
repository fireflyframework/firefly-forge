/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { ContinueRun } from "./ExecutionRecord";
import type { ForgeRun } from "./api";
const run: ForgeRun = { id: "a".repeat(32), repository_id: "b".repeat(32), brief: "A controlled decision fixture", status: "paused", agent: "codex", worker_request: { id: "ownership", kind: "design", status: "pending" }, pause: { kind: "worker_request", message: "A decision is needed", automatic: false } };
const render = (value: ForgeRun) => renderToStaticMarkup(<ContinueRun run={value} connection={{ base: "/api/v1", token: "" }} onUpdated={() => undefined} />);
it("holds resume and provider handoff while a human decision is pending", () => {
  const html = render(run);
  expect(html).toContain("Waiting for your answer");
  expect(html).toContain("Open agent questions");
  expect(html).not.toContain("Resume run");
  expect(html).not.toContain("Hand off run");
});
it("keeps explicit continuation available only after the answer is recorded", () => {
  expect(render({ ...run, worker_request: { ...run.worker_request!, status: "answered" } })).toContain("Resume run");
});
