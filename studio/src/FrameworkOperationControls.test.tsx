/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { renderToStaticMarkup } from "react-dom/server";
import { expect, it } from "vitest";
import { FrameworkOperationControls } from "./FrameworkOperationControls";
import { corpus, counts } from "../tests/framework-knowledge-fixture";
import type { FrameworkOperation } from "./FrameworkKnowledgeData";

const operation: FrameworkOperation = {
  request_id: "4".repeat(32),
  framework: "pyfly",
  corpus,
  state: "needs_reconciliation",
  stage: "fetching",
  counts,
  spent_budget: { requests: 10, bytes: 1000, seconds: 5.5 },
  remaining_budget: { requests: 40, bytes: 9000, seconds: 94.5 },
  sanitized_error: null,
  uncertain_sources: 0,
  allowed_actions: ["renew_budget", "cancel", "resume"],
  revision: "5".repeat(64),
};

it("reviews new total allowances and retains the already-spent work", () => {
  const html = renderToStaticMarkup(
    <FrameworkOperationControls
      operation={operation}
      busy={false}
      onRenew={() => {}}
    />,
  );
  expect(html).toContain("Total requests");
  expect(html).toContain('value="50"');
  expect(html).toContain('value="10000"');
  expect(html).toContain('value="100"');
  expect(html).toContain("Already spent");
  expect(html).toContain("5.5 seconds");
  expect(html).toContain("Apply total allowance");
});

it("never offers mutating controls without both current actions and callbacks", () => {
  for (const props of [
    { operation: { ...operation, allowed_actions: [] }, onCancel: () => {} },
    { operation },
  ]) {
    const html = renderToStaticMarkup(
      <FrameworkOperationControls {...props} busy={false} />,
    );
    expect(html).not.toContain("<button");
    expect(html).not.toContain("<form");
  }
});

it("keeps uncertain submissions visible on cancellation and prevents uncertain resume", () => {
  const html = renderToStaticMarkup(
    <FrameworkOperationControls
      operation={{ ...operation, uncertain_sources: 2 }}
      busy={false}
      onCancel={() => {}}
      onResume={() => {}}
    />,
  );
  expect(html).toContain("2 uncertain sources remain");
  expect(html).toContain("Cancel remaining work");
  expect(html).toContain("Observe uncertain submissions before resuming");
  expect(html).toContain("disabled");
  expect(html).not.toContain("Reset");
});
