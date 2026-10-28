/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import type { ReactNode } from "react";
import { asRecord } from "./ExecutionFormat";

const identifier = (value: unknown): value is string =>
  typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
const digest = (value: unknown) =>
  typeof value === "string" && /^[a-f0-9]{64}$/.test(value);

export function ReadonlyObservationEvidence({
  value,
  children,
}: {
  value: unknown;
  children?: ReactNode;
}) {
  const record = asRecord(value);
  const request = asRecord(record?.request);
  const candidate = asRecord(record?.candidate);
  const requestId = record?.request_id;
  const duration = request?.valid_for_seconds;
  const reviewed =
    record?.version === 1 &&
    record.kind === "reviewed_readonly_observation_continuation" &&
    identifier(record.run_id) &&
    identifier(requestId) &&
    identifier(record.recovery_id) &&
    request?.version === 1 &&
    [
      "acknowledge_unavailable_observation",
      "acknowledge_no_original_callback_replay",
      "acknowledge_same_request_attempt_and_budgets",
      "acknowledge_finite_ownership_lease",
    ].every((key) => request[key] === true) &&
    candidate?.version === 1 &&
    candidate.kind === "readonly_observation_recovery_candidate" &&
    candidate.run_id === record.run_id &&
    candidate.request_id === record.request_id &&
    candidate.admission === "READY" &&
    candidate.execution_authorized === false &&
    candidate.review_basis === "administrator_reviewed_unavailable_read" &&
    digest(candidate.digest) &&
    request.candidate_digest === candidate.digest &&
    digest(record.read_proof_digest) &&
    candidate.read_proof_digest === record.read_proof_digest &&
    typeof duration === "number" &&
    Number.isSafeInteger(duration) &&
    duration >= 1 &&
    duration <= 7200;

  return (
    <section className="record-evidence">
      {reviewed ? (
        <>
          <h4>Unavailable read reviewed</h4>
          <p>
            An administrator approved continuing the same request with an
            explicit unavailable read result. Previously recorded results are
            reused; their tools are not repeated.
          </p>
          <p>
            The original unknown result remains unknown. Existing consumption
            and budgets remain charged. Worker receipts show subsequent
            execution.
          </p>
          <dl className="record-fields">
            <div>
              <dt>Original request</dt>
              <dd>
                <span className="record-value">{requestId}</span>
              </dd>
            </div>
            <div>
              <dt>Read observation</dt>
              <dd>Unavailable</dd>
            </div>
            <div>
              <dt>Time allowance</dt>
              <dd>{duration} seconds</dd>
            </div>
          </dl>
        </>
      ) : (
        <>
          <p role="status">Unavailable read review is incomplete.</p>
          <p>
            Review the original record and its administrator approval before
            continuing.
          </p>
        </>
      )}
      {children}
    </section>
  );
}
