/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useState } from "react";
import { request, type Connection, type ForgeRun } from "./api";
import { asRecord } from "./ExecutionFormat";
import { EvidenceView } from "./ExecutionPresentation";
import { VerificationHandoff } from "./VerificationHandoff";

function handoffChecks(journal: unknown, handoff: Record<string, unknown>) {
  const value = asRecord(journal);
  if (value?.valid !== true || !Array.isArray(value.entries)) return null;
  const entries = value.entries.flatMap((entry) => {
    const record = asRecord(entry);
    return record ? [record] : [];
  });
  const checks = entries.filter((entry) => {
    const payload = asRecord(entry.payload);
    return (
      entry.task_id === "implement" &&
      entry.kind === "verification_handoff_command" &&
      payload?.version === 1 &&
      payload.id === handoff.id &&
      payload.request_id === handoff.request_id &&
      Number.isSafeInteger(payload.command_index) &&
      Number(payload.command_index) >= 1 &&
      Number(payload.command_index) <= 20 &&
      Number.isSafeInteger(payload.exit_code) &&
      payload.outcome === "completed" &&
      typeof payload.execution_id === "string" &&
      /^[a-f0-9]{32}$/.test(payload.execution_id)
    );
  });
  const indices = checks.map((entry) => asRecord(entry.payload)!.command_index);
  if (new Set(indices).size !== indices.length) return null;
  return checks
    .sort(
      (a, b) =>
        Number(asRecord(a.payload)!.command_index) -
        Number(asRecord(b.payload)!.command_index),
    )
    .map((entry) => {
      const payload = asRecord(entry.payload)!;
      const runner = entries.find(
        (item) =>
          item.task_id === "implement" &&
          item.kind === "sandbox_intent" &&
          asRecord(item.payload)?.execution_id === payload.execution_id &&
          asRecord(item.payload)?.effect === "start",
      );
      const invocation = asRecord(asRecord(runner?.payload)?.request);
      const argv =
        invocation?.ordinal === payload.ordinal
          ? asRecord(invocation?.request)?.argv
          : undefined;
      const receipt = entries.find(
        (item) =>
          item.task_id === "implement" &&
          ["sandbox_receipt", "sandbox_reconcile"].includes(
            String(item.kind),
          ) &&
          asRecord(item.payload)?.execution_id === payload.execution_id &&
          asRecord(item.payload)?.effect === "start",
      );
      const evidence = asRecord(asRecord(receipt?.payload)?.evidence);
      return {
        command: `Check ${payload.command_index}`,
        ...(Array.isArray(argv) && argv.every((arg) => typeof arg === "string")
          ? { argv }
          : {}),
        exit_code: payload.exit_code,
        ...(evidence &&
        evidence.exit_code === payload.exit_code &&
        typeof evidence.output_excerpt === "string"
          ? { output: evidence.output_excerpt }
          : {}),
      };
    });
}

export function VerificationResults({
  run,
  journal,
}: {
  run: ForgeRun;
  journal?: unknown;
}) {
  const results = Array.isArray(run.verification)
    ? run.verification.map(
        (value, index) => [String(index + 1), value] as const,
      )
    : Object.entries(run.verification ?? {});
  const handoff = asRecord(run.verification_handoff);
  const identified =
    handoff?.version === 1 &&
    typeof handoff.request_id === "string" &&
    typeof handoff.id === "string" &&
    /^[a-f0-9]{32}$/.test(handoff.id);
  const checks = identified ? handoffChecks(journal, handoff!) : null;
  return (
    <div className="execution-record">
      {identified && (
        <section aria-label="Factory verification checks">
          <h3>Factory verification checks</h3>
          <p>
            These checks supplied evidence to the coding agent. They are
            separate from final run verification.
          </p>
          <VerificationHandoff value={handoff} />
          {checks?.length ? (
            checks.map((check, index) => (
              <EvidenceView key={index} value={check} advanced={false} />
            ))
          ) : (
            <p className="subtle">
              Command receipts are unavailable. Inspect the original execution
              journal.
            </p>
          )}
        </section>
      )}
      <section aria-label="Final run checks">
        <h3>Final run checks</h3>
        {results.length ? (
          results.map(([name, result], index) => (
            <EvidenceView
              key={index}
              value={result}
              title={
                Array.isArray(asRecord(result)?.argv) ||
                asRecord(result)?.command
                  ? undefined
                  : name
              }
            />
          ))
        ) : (
          <p className="subtle">
            No final run verification results recorded. Agent completion alone
            does not establish acceptance.
          </p>
        )}
      </section>
    </div>
  );
}

export function RunVerification({
  run,
  connection,
}: {
  run: ForgeRun;
  connection: Connection;
}) {
  const [journal, setJournal] = useState<unknown>(null);
  const [error, setError] = useState("");
  const handoffId = asRecord(run.verification_handoff)?.id;
  useEffect(() => {
    if (!handoffId) return;
    const controller = new AbortController();
    async function read() {
      try {
        const value = await request<unknown>(
          connection,
          `/runs/${encodeURIComponent(run.id)}/journal`,
          { signal: controller.signal },
        );
        if (!controller.signal.aborted) {
          setJournal(value);
          setError("");
        }
      } catch {
        if (!controller.signal.aborted) {
          setJournal(null);
          setError("Check receipts could not be read with current access.");
        }
      }
    }
    void read();
    const timer = ["failed", "succeeded", "cancelled"].includes(run.status)
      ? undefined
      : window.setInterval(() => void read(), 2500);
    return () => {
      controller.abort();
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [connection, run.id, run.status, handoffId]);
  return (
    <>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      <VerificationResults run={run} journal={journal} />
    </>
  );
}
