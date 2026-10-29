/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import {
  Copy,
  FileCode2,
  Terminal,
  Check,
  ChevronDown,
  Waypoints,
} from "lucide-react";
import {
  asRecord,
  boundedText,
  fieldLabel,
  parseDiff,
  tokenizeCode,
} from "./ExecutionFormat";
import "./ExecutionPresentation.css";
import { isPHPProfile, PHPProfileEvidence } from "./PHPProfileEvidence";
import { workerEvidenceImages } from "./WorkerEvidenceData";
import { VerificationHandoff } from "./VerificationHandoff";
import { ReadonlyObservationEvidence } from "./ReadonlyObservationEvidence";
import { VerificationReuseEvidence } from "./VerificationReuse";

export function TimelineEventView({
  event,
}: {
  event: Record<string, unknown>;
}) {
  const message = typeof event.message === "string" ? event.message : "";
  const stage = typeof event.stage === "string" ? event.stage : "";
  let structured: Record<string, unknown> | undefined;
  if (["stdout", "stderr"].includes(stage) && message.length <= 65536) {
    try {
      structured = asRecord(JSON.parse(message));
    } catch {
      /* Plain provider output remains text. */
    }
  }
  if (structured) {
    const type = structured.type;
    const item = asRecord(structured.item);
    if (
      ["item.started", "item.updated", "item.completed"].includes(
        String(type),
      ) &&
      item?.type === "command_execution" &&
      typeof item.command === "string"
    ) {
      return (
        <>
          <h3>
            {type === "item.completed" ? "Command finished" : "Command started"}
          </h3>
          <EvidenceView
            value={{
              command: item.command,
              ...(type === "item.completed" &&
              Number.isSafeInteger(item.exit_code)
                ? { exit_code: item.exit_code }
                : {}),
              ...(typeof item.aggregated_output === "string"
                ? { output: item.aggregated_output }
                : {}),
            }}
            advanced={false}
          />
        </>
      );
    }
    if (
      ["agent_message", "reasoning"].includes(String(item?.type)) &&
      typeof item?.text === "string"
    ) {
      const label =
        item.type === "reasoning" ? "Provider progress" : "Provider report";
      return (
        <>
          <h3>{label}</h3>
          <RecordedMessage value={item.text} label={label} />
        </>
      );
    }
    if (item?.type === "file_change" && Array.isArray(item.changes)) {
      return (
        <>
          <h3>Source changes reported</h3>
          <EvidenceView
            value={{
              files: item.changes.slice(0, 30).flatMap((change) => {
                const value = asRecord(change);
                return typeof value?.path === "string"
                  ? [
                      {
                        path: value.path,
                        ...(typeof value.kind === "string"
                          ? { change: value.kind }
                          : {}),
                      },
                    ]
                  : [];
              }),
            }}
            advanced={false}
          />
        </>
      );
    }
    const titles: Record<string, string> = {
      "thread.started": "Provider session started",
      "turn.started": "Provider task started",
      "turn.completed": "Provider task completed",
      "turn.failed": "Provider reported a failure",
      error: "Provider reported an error",
    };
    const title =
      typeof type === "string" && Object.hasOwn(titles, type)
        ? titles[type]
        : "Provider event";
    const error = asRecord(structured.error);
    return (
      <>
        <h3>{title}</h3>
        {typeof error?.message === "string" ? (
          <RecordedMessage value={error.message} label="Provider error" />
        ) : (
          <RecordedMessage value={message} label="Recorded provider event" />
        )}
      </>
    );
  }
  const providerOutput = ["stdout", "stderr"].includes(stage);
  const longMessage = message.length > 180 || message.includes("\n");
  const heading = providerOutput
    ? stage === "stderr"
      ? "Provider error output"
      : "Provider output"
    : longMessage
      ? fieldLabel(stage || "Run event")
      : message || String(event.type ?? event.event ?? "Run event");
  return (
    <>
      <h3>
        <BoundedLabel value={heading} limit={180} />
      </h3>
      {providerOutput || longMessage ? (
        providerOutput && !/^\s*(?:\{|\[)/.test(message) ? (
          <OutputView value={message} label={heading} />
        ) : (
          <RecordedMessage value={message} label={heading} />
        )
      ) : (
        <EvidenceView
          value={event}
          omitFields={[
            "message",
            "type",
            "event",
            "timestamp",
            "at",
            "created_at",
          ]}
        />
      )}
    </>
  );
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [state, setState] = useState("");
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("Copied");
    } catch {
      setState("Copy unavailable. Select the text to copy.");
    }
  }
  return (
    <span className="record-copy">
      <button
        type="button"
        className="secondary"
        onClick={() => void copy()}
        aria-label={label}
      >
        {state === "Copied" ? <Check size={14} /> : <Copy size={14} />}
        {state === "Copied" ? "Copied" : label}
      </button>
      <span className="record-copy-status" role="status">
        {state === "Copied" ? "" : state}
      </span>
    </span>
  );
}
function BoundedLabel({
  value,
  limit = 350,
  original = value,
}: {
  value: string;
  limit?: number;
  original?: string;
}) {
  const preview = boundedText(value, limit, 4);
  return (
    <span className="record-bounded-label">
      {preview.text}
      {preview.omitted > 0 && (
        <>
          <span className="record-label-limit">
            … ({preview.omitted.toLocaleString()} more characters)
          </span>
          <CopyButton value={original} label="Copy full text" />
        </>
      )}
    </span>
  );
}

export function OutputView({
  value,
  label = "Output",
  path,
  copyLabel = "Copy full output",
}: {
  value: string;
  label?: string;
  path?: string;
  copyLabel?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const preview = boundedText(
    value,
    expanded ? 100000 : 10000,
    expanded ? 1000 : 120,
  );
  const initiallyBounded = boundedText(value).omitted > 0;
  return (
    <section className="record-output" aria-label={label}>
      <div className="record-output-heading">
        <strong>{label}</strong>
        <CopyButton value={value} label={copyLabel} />
      </div>
      <pre tabIndex={0} aria-label={`${label} text`}>
        <code>
          {path
            ? preview.text.split("\n").map((line, index) => (
                <span className="record-code-line" key={index}>
                  <span className="record-code-number" aria-hidden="true">
                    {index + 1}
                  </span>
                  <span>
                    {tokenizeCode(line, path).map((token, tokenIndex) => (
                      <span
                        key={tokenIndex}
                        className={`code-token-${token.kind}`}
                      >
                        {token.text}
                      </span>
                    ))}
                  </span>
                  {"\n"}
                </span>
              ))
            : preview.text || "No output recorded."}
        </code>
      </pre>
      {(initiallyBounded || preview.omitted > 0) && (
        <div className="record-output-limit">
          <span>
            {preview.omitted > 0
              ? `${preview.omitted.toLocaleString()} characters outside this preview. Copy includes the full recorded output.`
              : "Showing the full recorded output."}
          </span>
          {initiallyBounded && (
            <button
              className="secondary"
              type="button"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? "Show less" : "Show more"}
              <ChevronDown size={14} />
            </button>
          )}
        </div>
      )}
    </section>
  );
}
export function AdvancedRecord({ value }: { value: unknown }) {
  return (
    <details className="record-advanced">
      <summary>Advanced record</summary>
      <OutputView
        value={JSON.stringify(value, null, 2) ?? String(value)}
        label="Raw record"
      />
    </details>
  );
}

function reportedWorker(value: Record<string, unknown> | undefined) {
  const feedback = asRecord(value?.feedback);
  const text = (value: unknown, maximum: number): value is string =>
    typeof value === "string" &&
    value.trim().length > 0 &&
    value.length <= maximum;
  const identifier = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$/;
  const protocol = feedback?.protocol;
  const allowance = protocol !== undefined;
  const fields = [
    "id",
    "role_id",
    "task_id",
    "attempt",
    "disposition",
    "summary",
    "evidence_paths",
    "request",
  ];
  if (allowance) fields.push("protocol", "allowance_id", "allowance_digest");
  if (
    !value ||
    Object.keys(value).sort().join(",") !== "feedback,summary" ||
    !text(value.summary, 8000) ||
    !feedback ||
    Object.keys(feedback).some((key) => !fields.includes(key)) ||
    !text(feedback.id, 80) ||
    !identifier.test(feedback.id) ||
    typeof feedback.role_id !== "string" ||
    ![
      "product-manager",
      "tech-lead",
      "architect",
      "implementer",
      "qa",
      "security",
      "docs",
    ].includes(String(feedback.role_id)) ||
    typeof feedback.task_id !== "string" ||
    ![
      "prepare",
      "implement",
      "verify",
      "checkpoint",
      "review",
      "publication",
    ].includes(String(feedback.task_id)) ||
    typeof feedback.disposition !== "string" ||
    !["completed", "failed", "blocked", "clarification"].includes(
      String(feedback.disposition),
    ) ||
    !text(feedback.summary, 8000) ||
    !Number.isSafeInteger(feedback.attempt) ||
    (feedback.attempt as number) < 1 ||
    (!allowance && (feedback.attempt as number) > 4) ||
    (allowance &&
      (typeof protocol !== "string" ||
        ![
          "native-provider-recovery-allowance-v1",
          "source-budget-continuation-v1",
        ].includes(String(protocol)) ||
        feedback.role_id !== "implementer" ||
        feedback.task_id !== "implement" ||
        (protocol === "native-provider-recovery-allowance-v1"
          ? feedback.attempt !== 5
          : (feedback.attempt as number) < 5 ||
            (feedback.attempt as number) > 32) ||
        typeof feedback.allowance_id !== "string" ||
        !/^[a-f0-9]{32}$/.test(feedback.allowance_id) ||
        typeof feedback.allowance_digest !== "string" ||
        !/^[a-f0-9]{64}$/.test(feedback.allowance_digest))) ||
    !Array.isArray(feedback.evidence_paths) ||
    feedback.evidence_paths.length > 100 ||
    !feedback.evidence_paths.every(
      (path) =>
        text(path, 300) &&
        !path.startsWith("/") &&
        !path.includes("\\") &&
        path
          .split("/")
          .every(
            (part) =>
              part.length > 0 &&
              !part.startsWith(".") &&
              !["node_modules", "__pycache__"].includes(part),
          ),
    )
  )
    return undefined;
  const request = asRecord(feedback.request);
  const needsAnswer = ["blocked", "clarification"].includes(
    String(feedback.disposition),
  );
  if (needsAnswer) {
    if (
      !request ||
      Object.keys(request).some(
        (key) =>
          !["id", "kind", "question", "context", "choices"].includes(key),
      ) ||
      !text(request.id, 80) ||
      !identifier.test(request.id) ||
      typeof request.kind !== "string" ||
      ![
        "product_definition",
        "architecture",
        "design",
        "provider",
        "verification",
        "approval",
        "other",
      ].includes(String(request.kind)) ||
      !text(request.question, 4000) ||
      !text(request.context, 4000) ||
      (request.choices !== undefined &&
        (!Array.isArray(request.choices) ||
          request.choices.length > 6 ||
          !request.choices.every((choice) => text(choice, 1000))))
    )
      return undefined;
  } else if (feedback.request !== undefined && feedback.request !== null)
    return undefined;
  return {
    summary: value.summary,
    detail: feedback.summary,
    disposition: String(feedback.disposition),
    task: String(feedback.task_id),
    attempt: feedback.attempt as number,
    paths: feedback.evidence_paths as string[],
    question: request?.question as string | undefined,
  };
}

export function RecordedMessage({
  value,
  label = "Recorded message",
}: {
  value: string;
  label?: string;
}) {
  const structured = /^\s*(?:\{|\[)/.test(value);
  let report: ReturnType<typeof reportedWorker>;
  if (structured && value.length <= 65536) {
    try {
      report = reportedWorker(asRecord(JSON.parse(value)));
    } catch {
      /* Incomplete previews remain available in details. */
    }
  }
  const preview = boundedText(value);
  return (
    <>
      {report ? (
        <>
          <p className="record-message">{report.summary}</p>
          {report.detail !== report.summary && (
            <p className="record-message">{report.detail}</p>
          )}
          <dl
            className="record-fields"
            aria-label="Provider-reported worker result"
          >
            <div>
              <dt>Reported result</dt>
              <dd>{fieldLabel(report.disposition)}</dd>
            </div>
            <div>
              <dt>Task</dt>
              <dd>{fieldLabel(report.task)}</dd>
            </div>
            <div>
              <dt>Attempt</dt>
              <dd>{report.attempt}</dd>
            </div>
          </dl>
          {report.paths.length > 0 && (
            <>
              <h4>Reported evidence paths</h4>
              <div
                className="record-list"
                role="list"
                aria-label="Reported evidence paths"
              >
                {report.paths.map((path, index) => (
                  <div key={index} role="listitem">
                    <code>{path}</code>
                  </div>
                ))}
              </div>
            </>
          )}
          {report.question && (
            <p className="record-message">{report.question}</p>
          )}
          <p className="subtle">
            This is the provider's report. Factory receipts and final checks
            determine the actual outcome.
          </p>
        </>
      ) : structured ? (
        <p>
          A structured recorded message is retained. Open advanced details to
          inspect its contents; it does not establish a successful result.
        </p>
      ) : (
        <p className="record-message">{preview.text}</p>
      )}
      {!structured && preview.omitted > 0 && (
        <p className="subtle">
          Only a bounded preview is shown. The full recorded text remains in
          advanced details.
        </p>
      )}
      {(structured || preview.omitted > 0) && (
        <details className="record-advanced">
          <summary>Advanced details</summary>
          <OutputView value={value} label={label} />
        </details>
      )}
    </>
  );
}

function ValueView({ value, depth }: { value: unknown; depth: number }) {
  if (value === null) return <code>null</code>;
  if (typeof value === "string")
    return /^\s*(?:\{|\[)/.test(value) ? (
      <RecordedMessage value={value} />
    ) : value.length > 350 || value.includes("\n") ? (
      <OutputView value={value} />
    ) : (
      <span className="record-value">{value}</span>
    );
  if (typeof value === "number" || typeof value === "boolean")
    return <code>{String(value)}</code>;
  if (depth >= 4) return <AdvancedRecord value={value} />;
  if (Array.isArray(value))
    return value.length ? (
      <div className="record-list">
        {value.slice(0, 30).map((item, index) => (
          <EvidenceView
            key={index}
            value={item}
            depth={depth + 1}
            advanced={false}
          />
        ))}
        {value.length > 30 && (
          <p className="subtle">
            Showing 30 of {value.length} items. The advanced record retains all
            items.
          </p>
        )}
      </div>
    ) : (
      <span className="subtle">No items recorded.</span>
    );
  if (asRecord(value))
    return <EvidenceView value={value} depth={depth + 1} advanced={false} />;
  return <span className="subtle">Unsupported value</span>;
}
function ProviderRefusalEvidence({
  value,
  advanced,
}: {
  value: unknown;
  advanced: boolean;
}) {
  const record = asRecord(value);
  const request = asRecord(record?.request);
  const evidence = asRecord(request?.evidence);
  const candidate = asRecord(record?.candidate);
  const usage = asRecord(candidate?.usage);
  const budget = (used: unknown, limit: unknown) => {
    if (typeof used !== "number" || !Number.isSafeInteger(used) || used < 0)
      return undefined;
    if (limit === null) return `${used} used · no configured limit`;
    return typeof limit === "number" &&
      Number.isSafeInteger(limit) &&
      limit > 0 &&
      used <= limit
      ? `${used} of ${limit}`
      : undefined;
  };
  const requests = budget(usage?.requests, usage?.request_limit);
  const tools = budget(usage?.tool_calls, usage?.tool_calls_limit);
  const commands = budget(usage?.commands, usage?.command_limit);
  const duration = request?.valid_for_seconds;
  if (
    record?.version !== 1 ||
    record.kind !== "reviewed_pre_admission_continuation" ||
    typeof record.request_id !== "string" ||
    !/^[a-f0-9]{32}$/.test(record.request_id) ||
    request?.version !== 1 ||
    ![
      "acknowledge_reviewed_provider_epoch",
      "acknowledge_no_original_response_or_tool_replay",
      "acknowledge_same_request_attempt_and_budgets",
      "acknowledge_finite_ownership_lease",
    ].every((key) => request[key] === true) ||
    evidence?.version !== 1 ||
    evidence.provider !== "bridge" ||
    evidence.classification !== "request_refused_before_admission" ||
    typeof evidence.model !== "string" ||
    !evidence.model ||
    typeof evidence.http_status !== "number" ||
    !Number.isInteger(evidence.http_status) ||
    evidence.http_status < 400 ||
    evidence.http_status > 499 ||
    !["admissions", "jobs", "provider_dispatches", "tool_callbacks"].every(
      (key) => evidence[key] === 0,
    ) ||
    candidate?.version !== 1 ||
    candidate.kind !== "claimed_pre_admission_recovery_candidate" ||
    candidate.request_id !== record.request_id ||
    candidate.admission !== "READY" ||
    candidate.execution_authorized !== false ||
    candidate.review_basis !== "administrator_reviewed_exact_provider_epoch" ||
    typeof duration !== "number" ||
    !Number.isSafeInteger(duration) ||
    duration < 1 ||
    !requests ||
    !tools ||
    !commands
  )
    return (
      <section className="record-evidence">
        <p role="status">Provider refusal review is incomplete.</p>
        <p>
          Review the original record and its administrator approval before
          continuing.
        </p>
        {advanced && <AdvancedRecord value={value} />}
      </section>
    );
  const fields = [
    ["Original request", record.request_id],
    ["Provider", "Agent bridge"],
    ["Model", evidence.model],
    ["Time allowance", `${duration} seconds`],
    ["Requests used", requests],
    ["Tool calls used", tools],
    ["Commands used", commands],
  ];
  return (
    <section className="record-evidence">
      <h4>Provider refusal reviewed</h4>
      <p>
        An administrator approved a finite continuation for the original request
        after reviewing the provider build and refusal evidence.
      </p>
      <p>
        Requests, tool calls and commands already used remain charged. Worker
        receipts show subsequent execution.
      </p>
      <dl className="record-fields">
        {fields.map(([label, text]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <span className="record-value">{text}</span>
            </dd>
          </div>
        ))}
      </dl>
      {advanced && <AdvancedRecord value={value} />}
    </section>
  );
}

function monitorOutcome(payload: Record<string, unknown> | undefined) {
  if (
    payload?.process_group_drained !== true ||
    !Number.isSafeInteger(payload.exit_code)
  )
    return "Cleanup not confirmed; reconciliation required";
  return (
    (
      {
        exited: "Exit observed; cleanup recorded",
        cancelled: "Process cancelled; cleanup recorded",
        lease_expired: "Lease expired; cleanup recorded",
        output_limit: "Log limit reached; cleanup recorded",
      } as Record<string, string>
    )[String(payload.status)] ?? "Process requires reconciliation"
  );
}

export function EvidenceView({
  value,
  title,
  kind,
  depth = 0,
  advanced = true,
  omitFields = [],
}: {
  value: unknown;
  title?: string;
  kind?: string;
  depth?: number;
  advanced?: boolean;
  omitFields?: string[];
}) {
  if (
    kind &&
    [
      "runner_monitor_spawn_intent",
      "runner_monitor_started",
      "runner_monitor_terminal",
    ].includes(kind)
  ) {
    const record = asRecord(value),
      request = asRecord(record?.request);
    if (
      record?.version !== 1 ||
      typeof record.monitor_id !== "string" ||
      !/^[a-f0-9]{32}$/.test(record.monitor_id) ||
      typeof record.owner_epoch !== "string" ||
      !/^[a-f0-9]{32}$/.test(record.owner_epoch)
    )
      return (
        <section className="record-evidence">
          <p role="status">
            Process evidence is incomplete. Review the retained record before
            continuing.
          </p>
          {advanced && <AdvancedRecord value={value} />}
        </section>
      );
    const label =
      kind === "runner_monitor_terminal"
        ? monitorOutcome(record)
        : kind === "runner_monitor_started"
          ? "Process started"
          : "Process requested";
    return (
      <section className="record-evidence">
        <h4>{label}</h4>
        <EvidenceView
          value={{
            monitor: record.monitor_id,
            owner_epoch: record.owner_epoch,
            ...(request ? { request } : {}),
            ...(Number.isSafeInteger(record.exit_code)
              ? { exit_code: record.exit_code }
              : {}),
            ...(Number.isSafeInteger(record.output_bytes)
              ? { output_bytes: record.output_bytes }
              : {}),
          }}
          advanced={false}
        />
        {advanced && <AdvancedRecord value={value} />}
      </section>
    );
  }
  if (kind === "provider_rejection_recovery")
    return <ProviderRefusalEvidence value={value} advanced={advanced} />;
  if (kind === "readonly_observation_recovery")
    return (
      <ReadonlyObservationEvidence value={value}>
        {advanced && <AdvancedRecord value={value} />}
      </ReadonlyObservationEvidence>
    );
  if (
    kind === "index_sanitation_intent" ||
    kind === "index_sanitation_receipt" ||
    kind === "workspace_observed_readmission"
  ) {
    const record = asRecord(value);
    const request = asRecord(record?.request);
    const readmission = kind === "workspace_observed_readmission";
    const candidate = asRecord(record?.candidate);
    const plan = asRecord(candidate?.index_plan);
    const effect = asRecord(record?.effect);
    const artifacts =
      kind === "index_sanitation_receipt" ? effect?.artifacts : plan?.artifacts;
    const duration = request?.valid_for_seconds;
    if (
      !record ||
      record.version !== 1 ||
      !request ||
      request.version !== 1 ||
      (readmission
        ? record.no_provider_dispatch !== true
        : record.no_queue_or_provider_dispatch !== true)
    )
      return (
        <section className="record-evidence">
          <p role="status">
            Recovery evidence is incomplete. Review the original record before
            continuing.
          </p>
          {advanced && <AdvancedRecord value={value} />}
        </section>
      );
    return (
      <section className="record-evidence">
        <h4>
          {readmission
            ? "Observed-effect readmission recorded"
            : kind === "index_sanitation_intent"
              ? "Index-only cache sanitation requested"
              : "Index-only cache sanitation receipt"}
        </h4>
        <p>
          {readmission
            ? "An administrator recorded fresh, finite consent for a previously observed environment change. The original source-control delivery remains unknown. Task execution has separate receipts."
            : "This record concerns only reviewed new test-cache entries in Git's index. It does not resume the run or confirm the original SCM delivery. Physical files and original task budgets must remain unchanged."}
        </p>
        {!readmission && (
          <p className="subtle">
            Inspect the correlated intent and receipt before treating the
            index-only effect as recorded.
          </p>
        )}
        <dl className="record-fields">
          <div>
            <dt>Scope</dt>
            <dd>
              {readmission
                ? "Previously observed change; no repeat"
                : "Git index only; physical files retained"}
            </dd>
          </div>
          {!readmission &&
            Array.isArray(artifacts) &&
            artifacts.length > 0 &&
            artifacts.length <= 16 && (
              <div>
                <dt>Reviewed cache entries</dt>
                <dd>{artifacts.length}</dd>
              </div>
            )}
          {typeof duration === "number" &&
            Number.isSafeInteger(duration) &&
            duration >= 1 &&
            duration <= 1800 && (
              <div>
                <dt>Consent allowance</dt>
                <dd>{duration} seconds</dd>
              </div>
            )}
        </dl>
        {advanced && <AdvancedRecord value={value} />}
      </section>
    );
  }
  if (
    kind === "verification_reuse_intent" ||
    kind === "verification_reuse_receipt"
  )
    return (
      <>
        <VerificationReuseEvidence kind={kind} value={value} />
        {advanced && <AdvancedRecord value={value} />}
      </>
    );
  if (
    kind?.startsWith("verification_handoff_") ||
    kind === "verification_continuation_intent"
  )
    return <VerificationHandoff kind={kind} value={value} />;
  if (kind === "worker_evidence") {
    let images;
    const manifest = asRecord(value);
    try {
      if (
        manifest?.version !== 1 ||
        typeof manifest.run_id !== "string" ||
        !/^[a-f0-9]{32}$/.test(manifest.run_id) ||
        typeof manifest.request_id !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(manifest.request_id)
      )
        throw new Error("Invalid screenshot record");
      images = workerEvidenceImages(manifest.images);
      if (!images.length) throw new Error("Empty screenshot record");
    } catch {
      return (
        <div className="record-evidence">
          <p>Screenshot record could not be verified.</p>
          {advanced && <AdvancedRecord value={value} />}
        </div>
      );
    }
    return (
      <div className="record-evidence">
        <h4>Retained decision screenshots</h4>
        <p>
          {images.length} {images.length === 1 ? "image saved" : "images saved"}{" "}
          with the agent’s question. These are recorded references, not product
          acceptance.
        </p>
        <ul>
          {images.map((image) => (
            <li key={image.id}>
              {image.filename} · {Math.ceil(image.bytes / 1024)} KB
            </li>
          ))}
        </ul>
        <p>
          Open Agent questions to view the images with your current project
          access.
        </p>
        {advanced && <AdvancedRecord value={value} />}
      </div>
    );
  }
  if (kind === "repair_proposal") {
    title ??= "Repair proposal";
    omitFields = [
      ...omitFields,
      "allowed",
      "reason",
      "context",
      "source_tree",
      "fingerprint",
      "policy_digest",
      "verification_receipt",
    ];
  }
  const record = asRecord(value);
  if (record && isPHPProfile(record))
    return (
      <div className="record-evidence">
        {title && (
          <h4>
            <BoundedLabel value={title} />
          </h4>
        )}
        <PHPProfileEvidence record={record} />
        <details className="record-advanced">
          <summary>Original workload output and report</summary>
          <OutputView
            value={
              typeof record.output === "string"
                ? record.output
                : "No output recorded."
            }
          />
        </details>
        {advanced && <AdvancedRecord value={value} />}
      </div>
    );
  if (!record)
    return (
      <div className="record-evidence">
        {title && (
          <h4>
            <BoundedLabel value={title} />
          </h4>
        )}
        <ValueView value={value} depth={depth} />
        {advanced && typeof value === "object" && (
          <AdvancedRecord value={value} />
        )}
      </div>
    );
  const args =
    Array.isArray(record.argv) &&
    record.argv.every((item) => typeof item === "string")
      ? (record.argv as string[])
      : undefined;
  const command =
    args
      ?.map((arg) => (/\s/.test(arg) ? JSON.stringify(arg) : arg))
      .join(" ") ||
    (typeof record.command === "string" ? record.command : undefined);
  const exit =
    typeof record.exit_code === "number" ? record.exit_code : undefined;
  const commandPreview = command
    ? boundedText(
        (args?.join(" ") || command).replace(/\s+/g, " ").trim(),
        80,
        4,
      )
    : undefined;
  const status =
    exit !== undefined
      ? exit === 0
        ? "Passed"
        : "Failed"
      : record.passed === true
        ? "Passed"
        : record.passed === false
          ? "Failed"
          : typeof record.status === "string"
            ? fieldLabel(record.status)
            : "Outcome not recorded";
  const outputs = ["output", "stdout", "stderr"].filter(
    (key) => typeof record[key] === "string",
  );
  const hidden = new Set(
    command
      ? [
          "argv",
          "command",
          "exit_code",
          "passed",
          "status",
          "duration_ms",
          ...outputs,
        ]
      : [],
  );
  const fields = Object.entries(record).filter(
    ([key]) => !hidden.has(key) && !omitFields.includes(key),
  );
  return (
    <article className={`record-evidence ${command ? "record-command" : ""}`}>
      {title && (
        <h4>
          <BoundedLabel value={title} />
        </h4>
      )}
      {command && (
        <>
          <div className="record-command-heading">
            <Terminal size={17} />
            <code>
              {commandPreview?.text}
              {commandPreview && commandPreview.omitted > 0 && "…"}
            </code>
            <span
              className={`record-outcome ${exit !== undefined ? (exit === 0 ? "passed" : "failed") : ""}`}
            >
              <BoundedLabel
                value={status}
                limit={180}
                original={
                  typeof record.status === "string" ? record.status : status
                }
              />
            </span>
          </div>
          {commandPreview && commandPreview.text !== command && (
            <details className="record-advanced">
              <summary>Full command</summary>
              <OutputView
                value={command}
                label="Command"
                copyLabel="Copy full command"
              />
            </details>
          )}
          <div className="record-command-meta">
            {exit !== undefined && <span>Exit {exit}</span>}
            {typeof record.duration_ms === "number" && (
              <span>
                {(record.duration_ms / 1000).toLocaleString(undefined, {
                  maximumFractionDigits: 3,
                })}{" "}
                s
              </span>
            )}
          </div>
          {outputs.map((key) => (
            <OutputView
              key={key}
              value={record[key] as string}
              label={
                key === "stderr"
                  ? "Standard error"
                  : key === "stdout"
                    ? "Standard output"
                    : "Command output"
              }
            />
          ))}
        </>
      )}
      {fields.length > 0 && (
        <dl className="record-fields">
          {fields.slice(0, 40).map(([key, item]) => (
            <div key={key}>
              <dt>
                <BoundedLabel
                  value={fieldLabel(key)}
                  limit={120}
                  original={key}
                />
              </dt>
              <dd>
                {key === "duration_ms" && typeof item === "number" ? (
                  `${(item / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 })} s`
                ) : ["status", "outcome", "disposition"].includes(key) &&
                  typeof item === "string" ? (
                  <BoundedLabel
                    value={item.replaceAll("_", " ")}
                    limit={180}
                    original={item}
                  />
                ) : ["code", "content"].includes(key) &&
                  typeof item === "string" ? (
                  <OutputView
                    value={item}
                    label={fieldLabel(key)}
                    path={
                      typeof record.path === "string"
                        ? record.path
                        : typeof record.name === "string"
                          ? record.name
                          : undefined
                    }
                  />
                ) : (
                  <ValueView value={item} depth={depth} />
                )}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {fields.length > 40 && (
        <p className="subtle">
          Additional fields are retained in the advanced record.
        </p>
      )}
      {!command && fields.length === 0 && (
        <p className="subtle">No evidence fields recorded.</p>
      )}
      {advanced && <AdvancedRecord value={value} />}
    </article>
  );
}
export type JournalIdentity = {
  kind: string;
  task_id: string;
  digest?: string;
  idempotency_key?: string;
  payload?: unknown;
};

function sameSanitationEvidence(
  left: unknown,
  right: unknown,
  depth = 0,
  budget = { remaining: 8192 },
): boolean {
  if (--budget.remaining < 0 || depth > 16) return false;
  if (left === null || right === null) return left === right;
  if (typeof left !== typeof right) return false;
  if (typeof left === "string") return left.length <= 16384 && left === right;
  if (typeof left === "number") return Number.isFinite(left) && left === right;
  if (typeof left === "boolean") return left === right;
  if (Array.isArray(left))
    return (
      Array.isArray(right) &&
      left.length <= 256 &&
      left.length === right.length &&
      left.every((item, index) =>
        sameSanitationEvidence(item, right[index], depth + 1, budget),
      )
    );
  const a = asRecord(left),
    b = asRecord(right);
  if (!a || !b) return false;
  const keys = Object.keys(a);
  return (
    keys.length <= 256 &&
    keys.length === Object.keys(b).length &&
    keys.every(
      (key) =>
        Object.hasOwn(b, key) &&
        sameSanitationEvidence(a[key], b[key], depth + 1, budget),
    )
  );
}

function sanitationReceiptMatches(
  intent: JournalIdentity,
  receipt: JournalIdentity,
) {
  const a = asRecord(intent.payload),
    b = asRecord(receipt.payload);
  const request = asRecord(a?.request),
    actor = asRecord(a?.actor),
    candidate = asRecord(a?.candidate);
  const identifier = (value: unknown) =>
    typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
  const hash = (value: unknown) =>
    typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
  if (
    !a ||
    !b ||
    !request ||
    !actor ||
    !candidate ||
    intent.kind !== "index_sanitation_intent" ||
    receipt.kind !== "index_sanitation_receipt" ||
    intent.task_id !== receipt.task_id ||
    !identifier(intent.idempotency_key) ||
    intent.idempotency_key !== receipt.idempotency_key ||
    request.request_id !== intent.idempotency_key ||
    request.version !== 1 ||
    a.version !== 1 ||
    typeof request.valid_for_seconds !== "number" ||
    !Number.isSafeInteger(request.valid_for_seconds) ||
    request.valid_for_seconds < 1 ||
    request.valid_for_seconds > 1800 ||
    !hash(intent.digest) ||
    b.intent_digest !== intent.digest ||
    !identifier(actor.user_id) ||
    !identifier(a.original_request_id) ||
    !hash(a.original_intent_digest) ||
    request.original_intent_digest !== a.original_intent_digest ||
    !hash(candidate.digest) ||
    request.candidate_digest !== candidate.digest ||
    !hash(a.public_patch_sha256) ||
    a.no_queue_or_provider_dispatch !== true ||
    typeof a.expires_at_ms !== "number" ||
    !Number.isSafeInteger(a.expires_at_ms) ||
    a.expires_at_ms <= 0
  )
    return false;
  if (
    ![
      "acknowledge_index_only_generated_artifacts",
      "acknowledge_preserve_files_source_and_budgets",
      "acknowledge_original_scm_outcome_unknown",
      "acknowledge_no_queue_or_provider_dispatch",
    ].every((key) => request[key] === true)
  )
    return false;
  return [
    "version",
    "request",
    "actor",
    "original_request_id",
    "original_intent_digest",
    "candidate",
    "expires_at_ms",
    "public_patch_sha256",
    "no_queue_or_provider_dispatch",
  ].every(
    (key) =>
      Object.hasOwn(a, key) &&
      Object.hasOwn(b, key) &&
      sameSanitationEvidence(a[key], b[key]),
  );
}
function agenticToolOutcome(kind: string, payload: Record<string, unknown>) {
  if (payload.version !== 1) return "Unsupported tool record";
  if (kind === "agentic_tool_result")
    return payload.status === "retained"
      ? "Result retained"
      : "Result not retained";
  if (kind === "agentic_tool_unknown")
    return "Result unavailable; review required";
  return "Tool call recorded";
}
function AgenticToolEvidence({
  kind,
  value,
  disposition,
}: {
  kind: string;
  value: Record<string, unknown>;
  disposition?: string;
}) {
  if (kind === "agentic_tool_effect_link") {
    const effect = asRecord(value.effect);
    const target =
      typeof effect?.digest === "string" && /^[a-f0-9]{64}$/.test(effect.digest)
        ? effect.digest
        : null;
    return (
      <article
        className="record-evidence record-command record-tool"
        aria-label="Related native effect"
      >
        <div className="record-command-heading">
          <Waypoints size={17} />
          <strong>Related native effect</strong>
        </div>
        {typeof effect?.kind === "string" && <p>{fieldLabel(effect.kind)}</p>}
        {target && (
          <a href={"#journal-record-" + target}>Find original journal record</a>
        )}
        <AdvancedRecord value={value} />
      </article>
    );
  }
  const names: Record<string, string> = {
    list_files: "List source files",
    read_file: "Read a source file",
    search_text: "Search source text",
    git_diff: "Inspect source changes",
    write_file: "Write a source file",
    execute_command: "Run a project command",
    edit_lines: "Edit source lines",
    knowledge_lookup: "Search project knowledge",
    read_run_evidence: "Read execution evidence",
  };
  const tool = typeof value.tool === "string" ? value.tool : "Agent tool";
  const fields = {
    ...(typeof value.effect === "string"
      ? { effect: fieldLabel(value.effect) }
      : {}),
    ...(typeof value.tool_call_id === "string"
      ? { tool_call: value.tool_call_id }
      : {}),
  };
  return (
    <article
      className="record-evidence record-command record-tool"
      aria-label="Agent tool evidence"
    >
      <div className="record-command-heading">
        <Waypoints size={17} />
        <strong>
          <BoundedLabel value={names[tool] ?? tool} />
        </strong>
        <span className="record-outcome">
          {disposition ?? agenticToolOutcome(kind, value)}
        </span>
      </div>
      <EvidenceView value={fields} advanced={false} />
      {kind === "agentic_tool_unknown" && (
        <p className="subtle">
          The original result is unavailable. The callback will not be repeated
          automatically. Review the retained execution before continuing.
        </p>
      )}
      {value.arguments !== undefined && (
        <EvidenceView
          title="Requested arguments"
          value={value.arguments}
          advanced={false}
        />
      )}
      {value.result !== undefined && (
        <EvidenceView
          title="Retained result"
          value={value.result}
          advanced={false}
        />
      )}
      {value.arguments_artifact !== undefined && (
        <EvidenceView
          title="Retained arguments artifact"
          value={value.arguments_artifact}
          advanced={false}
        />
      )}
      {value.result_artifact !== undefined && (
        <EvidenceView
          title="Retained result artifact"
          value={value.result_artifact}
          advanced={false}
        />
      )}
      {Array.isArray(value.parent_effects) &&
        value.parent_effects.length > 0 && (
          <details>
            <summary>Related native effects</summary>
            <EvidenceView value={value.parent_effects} advanced={false} />
          </details>
        )}
      {(value.source_before !== undefined ||
        value.source_after !== undefined) && (
        <details>
          <summary>Source before and after</summary>
          <EvidenceView
            value={{ before: value.source_before, after: value.source_after }}
            advanced={false}
          />
        </details>
      )}
      <AdvancedRecord value={value} />
    </article>
  );
}

function agenticToolDisposition(
  entry: JournalIdentity,
  entries: JournalIdentity[],
) {
  const payload = asRecord(entry.payload);
  if (
    payload?.version !== 1 ||
    typeof payload.custody_id !== "string" ||
    !/^[a-f0-9]{64}$/.test(payload.custody_id) ||
    entry.idempotency_key !== payload.custody_id
  )
    return "Unsupported tool record";
  const sameCall = (item: JournalIdentity) => {
    const value = asRecord(item.payload);
    return (
      item.task_id === entry.task_id &&
      item.idempotency_key === payload.custody_id &&
      value?.version === 1 &&
      [
        "custody_id",
        "tool_call_id",
        "tool",
        "invocation_id",
        "task_key",
        "task_attempt",
        "request_step",
        "arguments_sha256",
        "binding_sha256",
        "effect",
        "raw_arguments_sha256",
        "request_context_sha256",
        "checkpoint_digest",
      ].every((key) => value[key] === payload[key])
    );
  };
  const intents = entries.filter(
    (item) => item.kind === "agentic_tool_intent" && sameCall(item),
  );
  if (intents.length !== 1 || typeof intents[0].digest !== "string")
    return "Tool intent not matched";
  const outcomes = entries.filter(
    (item) =>
      ["agentic_tool_result", "agentic_tool_unknown"].includes(item.kind) &&
      sameCall(item) &&
      asRecord(item.payload)?.intent_digest === intents[0].digest,
  );
  if (outcomes.length > 1) return "Conflicting tool records; review required";
  if (entry.kind === "agentic_tool_intent")
    return outcomes.length
      ? agenticToolOutcome(outcomes[0].kind, asRecord(outcomes[0].payload)!)
      : "Result not retained";
  return outcomes.includes(entry)
    ? agenticToolOutcome(entry.kind, payload)
    : "Result not matched to intent";
}

export function ToolEvidence({
  kind,
  value,
  disposition,
}: {
  kind: string;
  value: unknown;
  disposition?: string;
}) {
  const record = asRecord(value);
  if (!record) return <EvidenceView value={value} />;
  if (
    [
      "agentic_tool_intent",
      "agentic_tool_result",
      "agentic_tool_unknown",
      "agentic_tool_effect_link",
    ].includes(kind)
  )
    return (
      <AgenticToolEvidence
        kind={kind}
        value={record}
        disposition={disposition}
      />
    );
  const names: Record<string, string> = {
    coding_configured_check: "Run configured project check",
    internet_search: "Internet search",
    "external_mcp:github:get_file_contents": "GitHub · Read repository files",
    "external_mcp:github:get_commit": "GitHub · Read a commit",
    "external_mcp:github:list_commits": "GitHub · List commits",
    "external_mcp:github:list_branches": "GitHub · List branches",
  };
  const tool =
    typeof record.tool === "string" ? record.tool : "Tool invocation";
  const outcome =
    (kind === "tool_unknown" ? disposition : undefined) ??
    toolOutcome({ kind, task_id: "", payload: record }) ??
    (kind === "tool_intent" ? "Tool request recorded" : "Outcome not recorded");
  const result = asRecord(record.result);
  const texts =
    Array.isArray(result?.text) &&
    result.text.every((item) => typeof item === "string")
      ? (result.text as string[])
      : null;
  const metadata = Object.fromEntries(
    Object.entries(record).filter(
      ([key]) =>
        ![
          "tool",
          "arguments",
          "source",
          "configuration",
          "result",
          "error",
          "status",
          "correlation_id",
          "decision",
        ].includes(key),
    ),
  );
  return (
    <article
      className="record-evidence record-command record-tool"
      aria-label="Tool evidence"
    >
      <div className="record-command-heading">
        <Waypoints size={17} />
        <strong>
          <BoundedLabel value={names[tool] ?? tool} />
        </strong>
        <span
          className={`record-outcome ${kind === "tool_receipt" && record.status === "succeeded" ? "passed" : kind === "tool_receipt" && record.status === "rejected" ? "failed" : ""}`}
        >
          {outcome}
        </span>
      </div>
      <div className="record-command-meta">
        {typeof record.correlation_id === "string" && (
          <span>
            Invocation{" "}
            <code>
              <BoundedLabel value={record.correlation_id} limit={64} />
            </code>
          </span>
        )}
      </div>
      <details className="record-tool-identity">
        <summary>Tool identity</summary>
        <code className="record-tool-identity-text">
          <BoundedLabel value={tool} limit={180} />
        </code>
        <CopyButton value={tool} label="Copy tool identity" />
      </details>
      {kind === "tool_unknown" && (
        <p className="subtle">
          {disposition?.startsWith("Superseded") ||
          disposition === "Abandoned without replay"
            ? "This is the original unknown outcome. The later correlated record is shown in the status above."
            : "The provider outcome was not admitted. Review its usage before another request."}
        </p>
      )}
      {kind === "tool_reconciled" &&
        record.decision === "abandoned_without_replay" && (
          <p className="subtle">
            This invocation will not be replayed. Abandonment does not establish
            that the provider delivered a result or incurred no charge.
          </p>
        )}
      {record.arguments !== undefined && (
        <EvidenceView
          title="Requested arguments"
          value={record.arguments}
          advanced={false}
        />
      )}
      {record.source !== undefined && (
        <EvidenceView
          title="Source proof"
          value={record.source}
          advanced={false}
        />
      )}
      {record.error !== undefined && (
        <EvidenceView
          title="Recorded error"
          value={record.error}
          advanced={false}
        />
      )}
      {texts ? (
        <>
          {texts.slice(0, 8).map((text, index) => (
            <OutputView
              key={index}
              label={
                texts.length === 1
                  ? "Untrusted provider output"
                  : `Untrusted provider output ${index + 1}`
              }
              value={text}
            />
          ))}
          {texts.length > 8 && (
            <p className="subtle">
              Showing 8 of {texts.length} text blocks. The advanced record
              retains every block.
            </p>
          )}
          <EvidenceView
            value={Object.fromEntries(
              Object.entries(result!).filter(([key]) => key !== "text"),
            )}
            advanced={false}
          />
        </>
      ) : (
        record.result !== undefined && (
          <EvidenceView
            title="Recorded result"
            value={record.result}
            advanced={false}
          />
        )
      )}
      {!!Object.keys(metadata).length && (
        <EvidenceView value={metadata} advanced={false} />
      )}
      <AdvancedRecord value={value} />
    </article>
  );
}
function toolOutcome(entry: JournalIdentity) {
  const payload = asRecord(entry.payload);
  if (!payload) return undefined;
  if (entry.kind === "tool_reconciled")
    return payload.decision === "abandoned_without_replay"
      ? "Abandoned without replay"
      : undefined;
  if (entry.kind === "tool_unknown")
    return "Outcome unknown; reconciliation required";
  if (entry.kind === "tool_receipt")
    return payload.status === "succeeded"
      ? "Succeeded receipt recorded"
      : payload.status === "rejected"
        ? "Rejected receipt recorded"
        : "Receipt recorded; status unknown";
  return undefined;
}
export function journalDisposition(
  entry: JournalIdentity,
  entries: JournalIdentity[],
) {
  if (entry.kind === "runner_monitor_terminal")
    return monitorOutcome(asRecord(entry.payload));
  if (entry.kind === "runner_monitor_started")
    return "Process started; cleanup pending";
  if (entry.kind === "runner_monitor_spawn_intent")
    return "Process request recorded; start not inferred";
  if (
    [
      "agentic_tool_intent",
      "agentic_tool_result",
      "agentic_tool_unknown",
    ].includes(entry.kind)
  )
    return agenticToolDisposition(entry, entries);
  if (
    entry.kind === "index_sanitation_intent" ||
    entry.kind === "index_sanitation_receipt"
  ) {
    const matched =
      entry.kind === "index_sanitation_intent"
        ? entries.some((receipt) => sanitationReceiptMatches(entry, receipt))
        : entries.some((intent) => sanitationReceiptMatches(intent, entry));
    return matched
      ? "Index-only receipt recorded; run not resumed"
      : "Outcome not recorded; review required";
  }
  if (entry.kind.startsWith("tool_")) {
    const correlation = asRecord(entry.payload)?.correlation_id;
    if (
      typeof correlation !== "string" ||
      !/^[a-f0-9]{32}$/.test(correlation) ||
      (entry.kind !== "tool_reconciled" &&
        entry.idempotency_key !== correlation)
    )
      return entry.kind === "tool_intent" ? "Outcome not recorded" : undefined;
    const outcomes = entries.filter(
      (item) =>
        item.task_id === entry.task_id &&
        asRecord(item.payload)?.correlation_id === correlation &&
        (item.kind === "tool_reconciled" ||
          item.idempotency_key === correlation) &&
        toolOutcome(item),
    );
    const latest = outcomes.at(-1);
    if (entry.kind === "tool_intent")
      return latest ? toolOutcome(latest) : "Outcome not recorded";
    if (entry.kind === "tool_unknown" && latest?.kind === "tool_receipt") {
      const status = asRecord(latest.payload)?.status;
      return status === "succeeded" || status === "rejected"
        ? `Superseded by a ${status} receipt`
        : "Superseded by a receipt with unknown status";
    }
    if (entry.kind === "tool_unknown" && latest?.kind === "tool_reconciled")
      return toolOutcome(latest);
    return toolOutcome(entry);
  }
  if (!["intent", "sandbox_intent"].includes(entry.kind)) return undefined;
  const receiptKind =
    entry.kind === "sandbox_intent" ? "sandbox_receipt" : "receipt";
  const matched = (item: JournalIdentity) =>
    Boolean(entry.idempotency_key) &&
    item.task_id === entry.task_id &&
    item.idempotency_key === entry.idempotency_key;
  if (entries.some((item) => matched(item) && item.kind === receiptKind))
    return "Receipt recorded";
  if (
    entry.kind === "sandbox_intent" &&
    entries.some((item) => matched(item) && item.kind === "sandbox_reconcile")
  )
    return "Reconciliation recorded";
  return "Outcome not recorded";
}
export function UnifiedDiff({ value }: { value: string }) {
  const [expanded, setExpanded] = useState(false);
  const bounded = boundedText(
    value,
    expanded ? 500000 : 200000,
    expanded ? 5000 : 2000,
  );
  const files = parseDiff(bounded.text);
  return (
    <div className="record-diff" aria-label="Git diff">
      <div className="record-diff-heading">
        <span>
          {files.length} {files.length === 1 ? "file" : "files"} in preview
        </span>
        <CopyButton value={value} label="Copy patch" />
      </div>
      {files.map((file, index) => (
        <section
          key={index}
          className="record-diff-file"
          aria-label={`Changes in ${boundedText(file.path, 256, 1).text}`}
        >
          <header>
            <FileCode2 size={17} />
            <strong>
              <BoundedLabel value={file.path} limit={256} />
            </strong>
            <span className="record-diff-count added">+{file.additions}</span>
            <span className="record-diff-count removed">−{file.deletions}</span>
          </header>
          <div
            className="record-diff-scroll"
            tabIndex={0}
            role="region"
            aria-label={`Code changes for ${boundedText(file.path, 256, 1).text}`}
          >
            <table>
              <caption className="sr-only">
                Unified changes. Added and removed lines include signs; old and
                new line numbers track each hunk.
              </caption>
              <thead>
                <tr>
                  <th scope="col">Old line</th>
                  <th scope="col">New line</th>
                  <th scope="col">Change</th>
                  <th scope="col">Code</th>
                </tr>
              </thead>
              <tbody>
                {file.lines.map((line, lineIndex) => (
                  <tr key={lineIndex} className={`record-diff-${line.kind}`}>
                    <td>{line.oldLine ?? ""}</td>
                    <td>{line.newLine ?? ""}</td>
                    <td
                      aria-label={
                        line.kind === "addition"
                          ? "Added"
                          : line.kind === "deletion"
                            ? "Removed"
                            : undefined
                      }
                    >
                      {line.kind === "addition"
                        ? "+"
                        : line.kind === "deletion"
                          ? "−"
                          : ""}
                    </td>
                    <td>
                      <code>
                        {line.kind === "metadata" || line.kind === "hunk"
                          ? line.text
                          : tokenizeCode(line.text, file.path).map(
                              (token, tokenIndex) => (
                                <span
                                  key={tokenIndex}
                                  className={`code-token-${token.kind}`}
                                >
                                  {token.text}
                                </span>
                              ),
                            )}
                      </code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ))}
      {bounded.omitted > 0 && (
        <div className="record-output-limit">
          <span>
            Preview limited. Copy patch retains the full diff; counts cover
            visible lines.
          </span>
          {!expanded && (
            <button
              type="button"
              className="secondary"
              onClick={() => setExpanded(true)}
            >
              Show more changes
            </button>
          )}
        </div>
      )}
    </div>
  );
}
