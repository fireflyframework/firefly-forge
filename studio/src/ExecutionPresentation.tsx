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
}: {
  value: string;
  label?: string;
  path?: string;
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
        <CopyButton value={value} label="Copy full output" />
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
function ValueView({ value, depth }: { value: unknown; depth: number }) {
  if (value === null) return <code>null</code>;
  if (typeof value === "string")
    return value.length > 350 || value.includes("\n") ? (
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
              <BoundedLabel value={command} limit={1000} />
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
  idempotency_key?: string;
  payload?: unknown;
};
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
  const names: Record<string, string> = {
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
