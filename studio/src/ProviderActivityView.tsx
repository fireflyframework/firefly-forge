/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileText, Loader2, Terminal, Wrench } from "lucide-react";
import { request, type Connection } from "./api";
import { OutputView } from "./ExecutionPresentation";
import { boundedText } from "./ExecutionFormat";
import {
  activityRecord,
  readActivityContent,
  type ActivityContentBlock,
} from "./ProviderActivityData";

type Props = {
  event: Record<string, unknown>;
  runId: string;
  actorId?: string;
  connection?: Connection;
};
export function ProviderActivityView({
  event,
  runId,
  actorId,
  connection,
}: Props) {
  let record;
  try {
    record = activityRecord(event, runId);
  } catch {
    return (
      <>
        <h3>Recorded event unavailable</h3>
        <p className="subtle">
          The event identity or payload summary could not be verified. Refresh
          the run to read its current record.
        </p>
      </>
    );
  }
  if (!record) return null;
  return (
    <ArchivedActivity
      key={JSON.stringify([
        connection?.base,
        connection?.token,
        actorId,
        runId,
        event.id,
        record.descriptor.descriptor_sha256,
      ])}
      record={record}
      preview={typeof event.message === "string" ? event.message : ""}
      connection={connection}
      actorId={actorId}
    />
  );
}
function ArchivedActivity({
  record,
  preview,
  connection,
  actorId,
}: {
  record: NonNullable<ReturnType<typeof activityRecord>>;
  preview: string;
  connection?: Connection;
  actorId?: string;
}) {
  const { descriptor: d, summary: s } = record;
  const [content, setContent] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  const verified = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (content !== null) verified.current?.querySelector("pre")?.focus();
  }, [content]);
  useEffect(
    () => () => {
      const controller = pending.current;
      pending.current = null;
      controller?.abort();
    },
    [],
  );
  async function load() {
    if (!connection || !actorId || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    const timer = window.setTimeout(() => controller.abort(), 30000);
    try {
      const result = await readActivityContent(
        d,
        (chunk, signal) =>
          request<unknown>(
            connection,
            `/runs/${d.run_id}/activity/${d.event_id}/content?expected_descriptor=${d.descriptor_sha256}&chunk=${chunk}`,
            { signal },
          ),
        controller.signal,
      );
      if (pending.current === controller) setContent(result);
    } catch {
      if (pending.current === controller)
        setError(
          "The recorded event could not be read and verified. Refresh the run to check access and try again.",
        );
    } finally {
      window.clearTimeout(timer);
      if (pending.current === controller) {
        pending.current = null;
        setBusy(false);
      }
    }
  }
  const command =
    s?.item_type === "command_execution" &&
    typeof s.command_preview === "string";
  const mcp = s?.item_type === "mcp_tool_call";
  const configuredCheck =
    mcp &&
    s.tool_server === "forge_coding" &&
    s.tool_name === "run_configured_check";
  const structured =
    !s && /^\s*(?:\{|\[\s*(?:\{|\[|"|\d|]|-|true|false|null))/.test(preview);
  const titles: Record<string, string> = {
    "thread.started": "Provider session started",
    "turn.started": "Provider task started",
    "turn.completed": "Provider task completed",
    "turn.failed": "Provider reported a failure",
    error: "Provider reported an error",
    "claude.result": "Provider result reported",
    "claude.result_error": "Provider reported a failure",
  };
  const title = mcp
    ? "Provider tool activity"
    : command
      ? s.event_type === "item.completed"
        ? "Provider command finished"
        : "Provider command started"
      : s?.item_type === "tool_use"
        ? "Tool request recorded"
        : s?.item_type === "tool_result"
          ? "Tool result reported"
          : s?.item_type === "provider_message"
            ? "Provider message recorded"
            : s && Object.hasOwn(titles, s.event_type)
              ? titles[s.event_type]
              : s?.item_type === "agent_message"
                ? "Provider report"
                : s?.item_type === "reasoning"
                  ? "Provider progress"
                  : structured
                    ? "Structured provider output recorded"
                    : d.stage === "stderr"
                      ? "Provider error output"
                      : "Provider output";
  const commandPreview = command
    ? boundedText(s.command_preview!, 180, 4)
    : null;
  const exit =
    command && s.event_type === "item.completed" ? s.exit_code : undefined;
  return (
    <section className="record-evidence" aria-label="Archived provider event">
      <h3>{title}</h3>
      {mcp ? (
        <div className="record-command record-tool">
          <div className="record-command-heading">
            <Wrench size={17} />
            <strong>
              {configuredCheck
                ? "Run configured project check"
                : `${s.tool_server} · ${s.tool_name}`}
            </strong>
          </div>
          {configuredCheck && (
            <details className="record-tool-identity">
              <summary>Tool identity</summary>
              <code className="record-tool-identity-text">
                {s.tool_server} · {s.tool_name}
              </code>
            </details>
          )}
          <p>
            {s.tool_status === "completed"
              ? "Provider reported completion"
              : s.tool_status === "failed"
                ? "Provider reported a failure"
                : "Provider reported an in-progress tool call"}
            {s.tool_exit_code !== undefined && (
              <> · Reported exit {s.tool_exit_code}</>
            )}
          </p>
          {s.output_preview !== undefined && (
            <OutputView
              value={s.output_preview}
              label="Provider-reported output preview"
              copyLabel="Copy output preview"
            />
          )}
        </div>
      ) : command ? (
        <div className="record-command">
          <div className="record-command-heading">
            <Terminal size={17} />
            <code aria-label="Command preview">
              {commandPreview?.text}
              {commandPreview && commandPreview.omitted > 0 ? "…" : ""}
            </code>
            <span
              className={`record-outcome ${exit === undefined ? "" : exit === 0 ? "passed" : "failed"}`}
            >
              {exit === undefined ? "Outcome not recorded" : `Exit ${exit}`}
            </span>
          </div>
          {commandPreview && commandPreview.omitted > 0 && (
            <details className="record-advanced">
              <summary>Recorded command preview</summary>
              <OutputView
                value={s.command_preview!}
                label="Command preview"
                copyLabel="Copy command preview"
              />
            </details>
          )}
          {typeof s.output_preview === "string" && (
            <OutputView
              value={s.output_preview}
              label="Output preview"
              copyLabel="Copy output preview"
            />
          )}
        </div>
      ) : s?.content_blocks ? (
        <>
          {s.content_blocks.map((block, index) => (
            <ClaudeBlock key={index} block={block} />
          ))}
          {!!s.omitted_blocks && (
            <p className="subtle">
              {s.omitted_blocks} additional content blocks
              {d.status === "available"
                ? " are available in the full recorded event."
                : " were omitted from this preview."}
            </p>
          )}
        </>
      ) : s?.item_type === "tool_use" || s?.item_type === "tool_result" ? (
        <ClaudeBlock block={s as ActivityContentBlock} />
      ) : structured ? (
        <p className="subtle">
          This event uses a structured format without a supported display.
          {d.status === "available" &&
            " Open the full recorded event to inspect its contents."}
        </p>
      ) : (
        <OutputView
          value={s?.output_preview ?? preview}
          label="Recorded preview"
          copyLabel="Copy preview"
        />
      )}
      {s?.preview_omitted && (
        <p className="subtle">
          {d.status === "available"
            ? "Preview only. Read the full recorded event for its retained contents."
            : "Only this preview was retained."}
        </p>
      )}
      <p className="subtle">
        {mcp
          ? "Native runner receipts and final verification are tracked separately."
          : "Recorded from the coding provider. Factory verification is tracked separately."}
      </p>
      {d.status === "budget_exceeded" ? (
        <p role="note">
          The full event was not recorded because the archive limit was reached.
          Its size and digest are retained.
        </p>
      ) : (
        <details className="record-advanced">
          <summary>
            Full recorded event · {d.sanitized_message_bytes.toLocaleString()}{" "}
            bytes
          </summary>
          <p className="subtle">
            This is the sanitized event received by forge. It is read on demand
            and checked against its recorded digest.
          </p>
          {content === null ? (
            <>
              <button
                type="button"
                className="secondary"
                onClick={() => void load()}
                disabled={busy || !connection || !actorId}
              >
                {busy ? (
                  <Loader2 size={16} className="spin" />
                ) : (
                  <FileText size={16} />
                )}
                {busy ? "Reading recorded event…" : "Read full recorded event"}
              </button>
              {!connection || !actorId ? (
                <p className="subtle">Sign in to read the complete event.</p>
              ) : null}
              {busy && (
                <p role="status">Reading and verifying the recorded content.</p>
              )}
              {error && <p role="alert">{error}</p>}
            </>
          ) : (
            <div ref={verified}>
              <OutputView
                value={content}
                label="Verified recorded event"
                copyLabel="Copy full recorded event"
              />
            </div>
          )}
        </details>
      )}
    </section>
  );
}

function ClaudeBlock({ block }: { block: ActivityContentBlock }) {
  return (
    <section
      className="record-command record-tool"
      aria-label={
        block.item_type === "tool_use"
          ? "Observed tool request"
          : block.item_type === "tool_result"
            ? "Observed tool result"
            : "Provider text"
      }
    >
      {block.item_type !== "agent_message" && (
        <>
          <div className="record-command-heading">
            <Wrench size={17} />
            <strong>
              {block.tool_name ??
                (block.item_type === "tool_use"
                  ? "Tool request"
                  : "Tool result")}
            </strong>
          </div>
          {block.item_id && (
            <details className="record-tool-identity">
              <summary>Tool call identity</summary>
              <code
                className="record-tool-identity-text"
                tabIndex={0}
                aria-label="Tool call identity text"
              >
                {block.item_id}
              </code>
            </details>
          )}
          <p className="subtle">
            {block.item_type === "tool_use"
              ? "The provider requested this tool; this is not an execution receipt."
              : block.tool_outcome === "reported_error"
                ? "Provider reported an error"
                : "Provider reported a result"}
          </p>
        </>
      )}
      {block.command_preview !== undefined && (
        <OutputView
          value={block.command_preview}
          label="Requested command preview"
          copyLabel="Copy requested command preview"
        />
      )}
      {block.output_preview !== undefined && (
        <OutputView
          value={block.output_preview}
          label="Recorded preview"
          copyLabel="Copy preview"
        />
      )}
    </section>
  );
}
