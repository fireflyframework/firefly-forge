/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
export const record = {
  id: "b".repeat(32),
  state: "verified",
  kind: "local_outcome",
  verified_at: "2026-10-14T12:00:00Z",
  expires_at: "2026-10-17T12:00:00Z",
  envelope: {
    version: 1,
    type: "local_outcome",
    correlation_id: "c".repeat(32),
    run_id: "d".repeat(32),
    task_id: "verify",
    role: "qa",
    purpose: "verification",
    question: "Verify inventory reservation",
    statement:
      "The reservation regression passed against the inspected source.",
    context: {
      repository_id: "a".repeat(32),
      project_id: "f".repeat(32),
      component_id: "inventory",
      framework: "pyfly",
      version: "26.10.06",
      dependency_digest: "a".repeat(64),
      source_tree: "e".repeat(40),
      query_digest: "b".repeat(64),
    },
    source: {
      base_revision: "a".repeat(40),
      head: "d".repeat(40),
      source_tree: "e".repeat(40),
    },
    citations: ["local:owned-verification"],
    receipts: [
      {
        kind: "receipt",
        key: "owned:verify",
        sequence: 6,
        digest: "c".repeat(64),
      },
    ],
  },
};
