/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  MessageSquare,
  RefreshCw,
  Send,
} from "lucide-react";
import {
  ApiError,
  normalizeBase,
  post,
  request,
  type Connection,
  type ForgeRun,
} from "./api";
import type { IdentitySession } from "./Identity";
import { MarkdownEditor, RichMarkdown } from "./RichMarkdown";
import { attemptDigest } from "./LearningObservationAttemptData";
import {
  confirmReplyMarkers,
  readReplyMarkers,
  replyMarkerBinding,
  retainReplyMarker,
  workerReply,
  workerThreads,
  type ReplyContext,
  type ReplyMarker,
  type WorkerReply,
  type WorkerThread,
} from "./WorkerDialogueData";
import "./WorkerDialogue.css";

export function WorkerDialogue({
  run,
  connection,
  session,
}: {
  run: ForgeRun;
  connection: Connection;
  session: IdentitySession;
}) {
  const [threads, setThreads] = useState<WorkerThread[]>([]),
    [markers, setMarkers] = useState<ReplyMarker[]>([]);
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [answer, setAnswer] = useState(""),
    [review, setReview] = useState<WorkerReply | null>(null);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false),
    [loaded, setLoaded] = useState(false),
    [held, setHeld] = useState(false);
  const generation = useRef(0),
    reviewTitle = useRef<HTMLHeadingElement>(null);
  const membership = session.memberships.find(
    (value) => value.project_id === run.project_id,
  );
  const canRead =
    session.global_admin || !!membership?.permissions.includes("read");
  const canReply =
    canRead &&
    (session.global_admin ||
      (!!membership?.permissions.includes("execute") &&
        (!run.publish || membership.permissions.includes("release"))));
  const pending = threads.find((thread) => !thread.response);
  const unresolved = markers.filter(
    (value) => !confirmed.has(replyMarkerBinding(value)),
  );
  const scope: ReplyContext = {
    base: new URL(
      normalizeBase(connection.base),
      window.location.origin,
    ).href.replace(/\/$/, ""),
    actor: session.user.id,
    run: run.id,
    repository: run.repository_id,
    project: run.project_id ?? null,
  };
  const scopeKey = JSON.stringify([
    scope,
    connection.token,
    canRead,
    canReply,
    run.status,
    run.publish,
  ]);
  const currentScope = useRef(scopeKey);
  currentScope.current = scopeKey;
  async function read(cycle: number, binding: string) {
    const saved = readReplyMarkers(localStorage, scope);
    const value = await request<unknown>(
      connection,
      `/runs/${encodeURIComponent(run.id)}/worker-messages`,
    );
    const parsed = await workerThreads(value, run.id);
    const exact = await confirmReplyMarkers(saved, parsed);
    if (generation.current !== cycle || currentScope.current !== binding)
      return;
    setThreads(parsed);
    setMarkers(saved);
    setConfirmed(exact);
    setLoaded(true);
    setError("");
    if (saved.some((item) => exact.has(replyMarkerBinding(item))))
      setNotice("Answer recorded. The run stays paused until you resume it.");
    else if (saved.length)
      setNotice(
        "No matching answer is currently available. Keep the original request; this does not prove it was not sent.",
      );
  }
  function failed(cause: unknown, cycle: number, binding: string) {
    if (generation.current !== cycle || currentScope.current !== binding)
      return;
    setThreads([]);
    setReview(null);
    setAnswer("");
    setLoaded(false);
    setNotice("");
    setError(
      cause instanceof ApiError && [401, 403].includes(cause.status)
        ? "Your current session cannot read this conversation. Sign in or ask the project owner to review your access."
        : "The conversation could not be confirmed. Keep any original answer request; do not send another answer until it is reconciled.",
    );
  }
  useEffect(() => {
    const cycle = ++generation.current;
    setThreads([]);
    setMarkers([]);
    setConfirmed(new Set());
    setReview(null);
    setAnswer("");
    setError("");
    setNotice("");
    setLoaded(false);
    setHeld(false);
    if (!canRead) return;
    setBusy(true);
    void read(cycle, scopeKey)
      .catch((cause) => failed(cause, cycle, scopeKey))
      .finally(() => {
        if (generation.current === cycle) setBusy(false);
      });
    return () => {
      generation.current = cycle + 1;
    };
    // A binding change invalidates every outstanding private read and draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scopeKey]);
  useEffect(() => {
    if (review) reviewTitle.current?.focus();
  }, [review]);
  async function refresh() {
    const cycle = generation.current,
      binding = scopeKey;
    setBusy(true);
    try {
      await read(cycle, binding);
    } catch (cause) {
      failed(cause, cycle, binding);
    } finally {
      if (generation.current === cycle && currentScope.current === binding)
        setBusy(false);
    }
  }
  function prepare() {
    if (
      !pending ||
      !canReply ||
      unresolved.length ||
      held ||
      busy ||
      !loaded ||
      run.status !== "paused"
    )
      return;
    try {
      const latest = readReplyMarkers(localStorage, scope);
      setMarkers(latest);
      if (latest.some((value) => !confirmed.has(replyMarkerBinding(value)))) {
        setError(
          "An earlier answer request is unresolved or has changed. Check its original result before reviewing another answer.",
        );
        return;
      }
    } catch {
      setHeld(true);
      setError(
        "The original answer identities could not be read. Check the recorded conversation before continuing.",
      );
      return;
    }
    try {
      setReview(
        workerReply(
          crypto.randomUUID().replaceAll("-", ""),
          session.user.display_name || "Project operator",
          answer,
        ),
      );
      setError("");
    } catch {
      setError("Write an answer of 1–8,000 characters before reviewing it.");
    }
  }
  async function send() {
    if (
      !review ||
      !pending ||
      !canReply ||
      unresolved.length ||
      held ||
      !loaded ||
      run.status !== "paused" ||
      busy
    )
      return;
    const cycle = generation.current,
      binding = scopeKey;
    setBusy(true);
    setError("");
    let saved: ReplyMarker;
    try {
      const inputDigest = await attemptDigest({
        ...review,
        request_id: pending.question.id,
      });
      if (generation.current !== cycle || currentScope.current !== binding)
        return;
      saved = retainReplyMarker(
        localStorage,
        {
          ...scope,
          version: 1,
          requestId: pending.question.id,
          responseId: review.id,
          questionDigest: pending.event.digest,
          inputDigest,
        },
        confirmed,
      );
      setMarkers((current) => [...current, saved]);
    } catch {
      if (generation.current === cycle && currentScope.current === binding) {
        setHeld(true);
        setError(
          "The answer identity could not be safely retained. Nothing further will be sent from this form; review the original conversation before continuing.",
        );
        setBusy(false);
      }
      return;
    }
    const original = review;
    setReview(null);
    setAnswer("");
    try {
      await post<unknown>(
        connection,
        `/runs/${encodeURIComponent(run.id)}/worker-requests/${encodeURIComponent(saved.requestId)}/respond`,
        original,
      );
      await read(cycle, binding);
    } catch (cause) {
      if (generation.current === cycle && currentScope.current === binding) {
        if (cause instanceof ApiError && [401, 403].includes(cause.status))
          failed(cause, cycle, binding);
        else
          setNotice(
            "Delivery is unconfirmed. Check the original answer; another answer will not be sent automatically.",
          );
      }
    } finally {
      if (generation.current === cycle && currentScope.current === binding)
        setBusy(false);
    }
  }
  if (!canRead)
    return (
      <p>
        Your current project access does not allow this conversation to be read.
      </p>
    );
  return (
    <section
      className="worker-dialogue"
      aria-label="Agent questions and answers"
    >
      <header className="worker-dialogue-heading">
        <div>
          <span className="worker-eyebrow">
            <MessageSquare size={18} /> Human decisions
          </span>
          <h2>Agent questions</h2>
          <p>
            Review what the agent needs, record your answer, then resume the run
            when you are ready.
          </p>
        </div>
        <button
          className="secondary"
          disabled={busy}
          onClick={() => void refresh()}
        >
          <RefreshCw size={16} />
          {markers.length ? "Check original answer" : "Refresh conversation"}
        </button>
      </header>
      {error && (
        <p className="alert" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="worker-reply-notice" role="status">
          <CheckCircle2 size={18} />
          {notice}
        </p>
      )}
      {busy && <p role="status">Reading the recorded conversation…</p>}
      {loaded && !threads.length && (
        <p>No agent has requested a decision for this run.</p>
      )}
      {threads.map((thread) => (
        <article key={thread.question.id} className="worker-thread">
          <div className="worker-thread-meta">
            <span>{thread.response ? "Answered" : "Needs your decision"}</span>
            <span>
              {String(thread.event.payload.role_id).replaceAll("_", " ")} ·{" "}
              {thread.event.task_id}
            </span>
          </div>
          <h3>{thread.question.question}</h3>
          <details>
            <summary>Why the agent is asking</summary>
            <RichMarkdown value={thread.question.context} />
          </details>
          {thread.response ? (
            <section
              className="worker-recorded-answer"
              aria-label="Recorded answer"
            >
              <h4>Recorded answer</h4>
              <RichMarkdown value={String(thread.response.payload.answer)} />
              <p className="subtle">
                Recorded label: {String(thread.response.payload.actor)}.{" "}
                {thread.response.payload.responding_actor
                  ? "The server also recorded the authenticated user identity."
                  : "This label does not establish authenticated authorship."}
              </p>
            </section>
          ) : !canReply ? (
            <p>
              Answering requires execute access
              {run.publish ? " and release access" : ""} for this project.
            </p>
          ) : run.status !== "paused" ? (
            <p>This run must be paused before it can receive an answer.</p>
          ) : review ? (
            <section className="worker-answer-review">
              <h4 ref={reviewTitle} tabIndex={-1}>
                Review your answer
              </h4>
              <RichMarkdown value={review.answer} />
              <p>
                The answer is added to the immutable execution record. Keep
                passwords, API keys and other secrets out of it.
              </p>
              <p>
                Sending an answer keeps the run paused. Resume it separately
                after reviewing the result.
              </p>
              <div className="worker-dialogue-actions">
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setReview(null)}
                >
                  <ArrowLeft size={16} />
                  Edit answer
                </button>
                <button
                  className="primary"
                  disabled={busy || held || !!unresolved.length}
                  onClick={() => void send()}
                >
                  <Send size={16} />
                  Send answer
                </button>
              </div>
            </section>
          ) : (
            !unresolved.length &&
            !held && (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  prepare();
                }}
                className="worker-answer-form"
              >
                {!!thread.question.choices.length && (
                  <div
                    className="worker-answer-choices"
                    aria-label="Suggested answers"
                  >
                    {thread.question.choices.map((choice, index) => (
                      <button
                        type="button"
                        className="secondary"
                        key={index}
                        onClick={() => setAnswer(choice)}
                      >
                        {choice}
                      </button>
                    ))}
                  </div>
                )}
                <MarkdownEditor
                  id="worker-answer"
                  label="Your answer"
                  value={answer}
                  required
                  onChange={setAnswer}
                  hint="Use a suggested answer or write your own. Keep secrets out of the execution record."
                />
                <div className="worker-dialogue-actions">
                  <button
                    className="primary"
                    disabled={busy || !answer}
                    type="submit"
                  >
                    Review answer
                  </button>
                </div>
              </form>
            )
          )}
          <details className="worker-event-reference">
            <summary>Execution record references</summary>
            <dl>
              <dt>Question</dt>
              <dd>
                <code>{thread.question.id}</code>
              </dd>
              <dt>Question event</dt>
              <dd>
                <code>{thread.event.digest}</code>
              </dd>
              {thread.response && (
                <>
                  <dt>Answer event</dt>
                  <dd>
                    <code>{thread.response.digest}</code>
                  </dd>
                </>
              )}
            </dl>
            <p>
              Each displayed event digest is checked separately. These filtered
              events do not verify the complete journal chain.
            </p>
          </details>
        </article>
      ))}
    </section>
  );
}
