/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import { FileText, Loader2, Terminal } from "lucide-react";
import { request, type Connection } from "./api";
import { OutputView } from "./ExecutionPresentation";
import { boundedText } from "./ExecutionFormat";
import { activityRecord, readActivityContent } from "./ProviderActivityData";

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
  const titles: Record<string, string> = {
    "thread.started": "Provider session started",
    "turn.started": "Provider task started",
    "turn.completed": "Provider task completed",
    "turn.failed": "Provider reported a failure",
    error: "Provider reported an error",
  };
  const title = command
    ? s.event_type === "item.completed"
      ? "Provider command finished"
      : "Provider command started"
    : s?.item_type === "agent_message"
      ? "Provider report"
      : s?.item_type === "reasoning"
        ? "Provider progress"
        : s && Object.hasOwn(titles, s.event_type)
          ? titles[s.event_type]
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
      {command ? (
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
      ) : (
        <OutputView
          value={s?.output_preview ?? preview}
          label="Recorded preview"
          copyLabel="Copy preview"
        />
      )}
      {s?.preview_omitted && (
        <p className="subtle">
          Preview only. Read the full recorded event for the retained command
          and output.
        </p>
      )}
      <p className="subtle">
        Recorded from the coding provider. Factory verification is tracked
        separately.
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
