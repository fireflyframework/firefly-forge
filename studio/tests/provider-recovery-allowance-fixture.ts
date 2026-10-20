/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
// Controlled HTTP presentation fixtures. They do not establish a live CLI result or WAL acceptance.
export const context = {
  base: "https://factory.example/api/v1",
  actor: "f".repeat(32),
  authentication: "local_bootstrap",
  run: "2".repeat(32),
  repository: "3".repeat(32),
  project: null,
  agent: "codex" as const,
};
export const candidate = {
  version: 1,
  protocol: "native-provider-recovery-allowance-v1",
  operator_digest: "a".repeat(64),
  run_id: context.run,
  agent: "codex",
  config_revision: "b".repeat(32),
  failed_task_key: "4".repeat(32),
  failed_intent_digest: "b".repeat(64),
  failure_digest: "c".repeat(64),
  journal_digest: "d".repeat(64),
  dispatches_used: 4,
  next_attempt: 5,
  process_evidence: "native_cli_process_receipt",
  native_process_digest: "e".repeat(64),
  cut: {
    source_digest: "c".repeat(64),
    git_digest: "a".repeat(64),
    index_digest: "b".repeat(64),
    metadata_digest: "d".repeat(64),
    scope_digest: "e".repeat(64),
    invocation_digest: "f".repeat(64),
    commands_digest: "1".repeat(64),
    policy_digest: "2".repeat(64),
    provider_digest: "3".repeat(64),
    retained_state_digest: "4".repeat(64),
  },
  executable_adoption: {
    path: "/opt/firefly/codex",
    sha256: "f".repeat(64),
    dev: 1,
    ino: 2,
    size: 12345,
    mode: 33261,
    uid: 1000,
    gid: 1000,
    mtime_ns: 1792454400000000000,
    ctime_ns: 1792454400000000000,
  },
  digest: "9".repeat(64),
};
export async function receipt(body: Record<string, unknown>) {
  const bytes = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(
      JSON.stringify({
        protocol: candidate.protocol,
        request: body.request_id,
        run: context.run,
      }),
    ),
  );
  const taskKey = [...new Uint8Array(bytes)]
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 32);
  return {
    version: 2,
    protocol: candidate.protocol,
    request: body,
    candidate,
    task_key: taskKey,
    actor: {
      user_id: context.actor,
      authentication_method: "local_bootstrap",
      global_admin: true,
      api_audience: null,
      identity_digest: "a".repeat(64),
    },
    previous_task: {
      id: "implement",
      status: "failed",
      attempt: 4,
      idempotency_key: candidate.failed_task_key,
    },
    process_evidence: {
      provenance: "native_cli_process_receipt",
      digest: candidate.native_process_digest,
    },
    mitigation_evidence: {
      provenance: "administrator_attestation",
      digest: body.mitigation_evidence_digest,
    },
  };
}
