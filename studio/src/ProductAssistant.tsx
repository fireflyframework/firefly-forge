/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  MessageSquare,
  Paperclip,
  Plus,
  RefreshCw,
  Send,
  Settings2,
  PanelRightClose,
} from "lucide-react";
import {
  ApiError,
  request,
  type Connection,
  type AIProposalProvenance,
  type CustomerProductDefinition,
} from "./api";
import { AssistantRecovery, type RecoverableTurn } from "./AssistantRecovery";
import { StepContextReview } from "./StepContextReview";
import { RichMarkdown } from "./RichMarkdown";
import {
  AssistantSources,
  type AssistantSourceContext,
} from "./AssistantSources";
import {
  ConversationFiles,
  ConversationFileTray,
  attachmentSelectionError,
  type ConversationAttachment,
} from "./ConversationFiles";
type Turn = RecoverableTurn & {
  context?: AssistantSourceContext & { attachments?: ConversationAttachment[] };
  provenance?: AIProposalProvenance | null;
  id: string;
  request_id: string;
  message: string;
  status: string;
  detail?: string;
  failure_kind?: string;
  created_at: string;
  reply: {
    message: string;
    questions: string[];
    assumptions: string[];
    citation_ids: string[];
    proposal: CustomerProductDefinition | null;
  } | null;
};
const assistantTasks = {
  product: "Product definition",
  functions: "Features and journeys",
  design: "Design",
  architecture: "Architecture",
} as const;
type AssistantTask = keyof typeof assistantTasks;
type Conversation = {
  task?: AssistantTask;
  execution_config?: { model?: string };
  model_selection?: {
    profile_id: string;
    profile_revision: number;
    provider: string;
    task: AssistantTask;
    execution_config: { model?: string };
  } | null;
  id: string;
  repository_id: string;
  project_id?: string | null;
  agent: string;
  created_at: string;
  turns?: Turn[];
  journal?: { verified: boolean; head: string; entries: unknown[] };
};
const failure = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The conversation could not be read.";
export function ProductAssistant({
  connection,
  repository,
  projectId,
  onConfigure,
  onUse,
  sourceTarget,
  onCollapse,
  draftPrompt,
}: {
  draftPrompt?: { id: number; text: string };
  onCollapse?: () => void;
  sourceTarget?: { conversation_id: string; turn_id: string };
  connection: Connection;
  repository: string;
  projectId?: string;
  onConfigure: () => void;
  onUse: (
    definition: CustomerProductDefinition,
    source: AIProposalProvenance,
  ) => void;
}) {
  const [sessions, setSessions] = useState<Conversation[]>([]),
    [selected, setSelected] = useState(""),
    [conversation, setConversation] = useState<Conversation | null>(null),
    [task, setTask] = useState<AssistantTask>("product"),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [revision, setRevision] = useState(0),
    [uncertain, setUncertain] = useState<string | null>(null);
  const [recoveryTurn, setRecoveryTurn] = useState<Turn | null>(null);
  const [imageCapability, setImageCapability] = useState<{
    conversation: string;
    status: string;
  } | null>(null);
  const [contextReview, setContextReview] = useState<string | null>(null);
  const [contextNote, setContextNote] = useState(false);
  const lastPrompt = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (draftPrompt && draftPrompt.id !== lastPrompt.current) {
      lastPrompt.current = draftPrompt.id;
      if (message.trim()) setContextReview(draftPrompt.text);
      else {
        setMessage(draftPrompt.text);
        setContextNote(true);
      }
    }
  }, [draftPrompt, message]);
  const [filesOpen, setFilesOpen] = useState(false);
  const [fileDrafts, setFileDrafts] = useState<
    Record<string, ConversationAttachment[]>
  >({});
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  useEffect(() => {
    const controller = new AbortController();
    request<Conversation[]>(
      connection,
      `/repositories/${repository}/assistant/conversations`,
      { signal: controller.signal },
    )
      .then((values) => {
        if (controller.signal.aborted) return;
        if (!Array.isArray(values))
          throw new Error(
            "The conversation list could not be read. Refresh the recorded conversations.",
          );
        setError("");
        setSessions(
          values.filter(
            (item) => (item.project_id ?? null) === (projectId ?? null),
          ),
        );
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(failure(cause));
      });
    return () => controller.abort();
  }, [connection, repository, projectId, revision]);
  const id = sessions.some((item) => item.id === selected)
    ? selected
    : sessions[0]?.id;
  useEffect(() => {
    if (!id) return;
    const controller = new AbortController();
    const refresh = () =>
      request<Conversation>(
        connection,
        `/repositories/${repository}/assistant/conversations/${id}`,
        { signal: controller.signal },
      )
        .then((value) => {
          if (controller.signal.aborted) return;
          setConversation(value);
          setError("");
          if (
            uncertain &&
            value.turns?.some((turn) => turn.request_id === uncertain)
          )
            setUncertain(null);
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setError(failure(cause));
        });
    void refresh();
    const timer = setInterval(() => void refresh(), 3000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [connection, repository, id, revision, uncertain]);
  useEffect(() => {
    setImageCapability(null);
    setRecoveryTurn(null);
    if (!id) return;
    const controller = new AbortController();
    request<{ image_input: { status: string } }>(
      connection,
      `/repositories/${repository}/assistant/conversations/${id}/capabilities`,
      { signal: controller.signal },
    )
      .then((value) => {
        if (!controller.signal.aborted)
          setImageCapability({
            conversation: id,
            status: value.image_input?.status ?? "unverified",
          });
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setImageCapability({ conversation: id, status: "unverified" });
      });
    return () => controller.abort();
  }, [connection, repository, id, revision]);
  useEffect(() => {
    if (sourceTarget) setSelected(sourceTarget.conversation_id);
  }, [sourceTarget]);
  const current = conversation?.id === id ? conversation : null;
  const selectedFiles = current ? (fileDrafts[current.id] ?? []) : [];
  const fileError = attachmentSelectionError(selectedFiles);
  function selectFiles(
    conversationId: string,
    files: ConversationAttachment[],
  ) {
    setFileDrafts((items) => ({ ...items, [conversationId]: files }));
  }
  const recordedModel =
    current?.model_selection?.execution_config.model ||
    current?.execution_config?.model;
  const targetReady = Boolean(
    sourceTarget &&
    current?.id === sourceTarget.conversation_id &&
    current.turns?.some((turn) => turn.id === sourceTarget.turn_id),
  );
  const positionedConversation = useRef<{
    id: string;
    target: typeof sourceTarget;
  } | null>(null);
  useEffect(() => {
    const turnId =
      targetReady && sourceTarget
        ? sourceTarget.turn_id
        : current?.turns?.at(-1)?.id;
    if (
      !turnId ||
      !current ||
      (positionedConversation.current?.id === current.id &&
        positionedConversation.current.target === sourceTarget)
    )
      return;
    const node = document.getElementById(`assistant-turn-${turnId}`),
      scroll = node?.closest<HTMLElement>(".assistant-turns");
    if (!node || !scroll) return;
    scroll.scrollTop +=
      node.getBoundingClientRect().top - scroll.getBoundingClientRect().top;
    positionedConversation.current = { id: current.id, target: sourceTarget };
  }, [targetReady, sourceTarget, current]);

  const active = Boolean(
    current?.turns?.some((turn) => turn.status === "processing"),
  );
  async function create() {
    const controller = new AbortController();
    pending.current?.abort();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await request<Conversation>(
        connection,
        `/repositories/${repository}/assistant/conversations`,
        {
          method: "POST",
          body: JSON.stringify({
            task,
            ...(projectId ? { project_id: projectId } : {}),
          }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setSessions((items) => [value, ...items]);
      setSelected(value.id);
      setConversation(null);
      setUncertain(null);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (!current || !message.trim() || active || uncertain || fileError) return;
    const controller = new AbortController();
    pending.current?.abort();
    pending.current = controller;
    const requestId = crypto.randomUUID().replaceAll("-", "");
    setBusy(true);
    setError("");
    try {
      await request<Turn>(
        connection,
        `/repositories/${repository}/assistant/conversations/${current.id}/turns`,
        {
          method: "POST",
          body: JSON.stringify({
            request_id: requestId,
            message: message.trim(),
            ...(selectedFiles.length
              ? { attachment_ids: selectedFiles.map((file) => file.id) }
              : {}),
          }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setMessage("");
      setContextNote(false);
      selectFiles(current.id, []);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) {
        const rejected =
          cause instanceof ApiError && [409, 422].includes(cause.status);
        if (!rejected) setUncertain(requestId);
        setError(
          rejected
            ? failure(cause)
            : `${failure(cause)} Refresh the recorded conversation before requesting another turn.`,
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="product-assistant">
      <header className="feature-toolbar">
        <div>
          <h2>
            <MessageSquare size={24} />
            AI design assistant
          </h2>
        </div>
        <button
          className="quiet"
          disabled={busy}
          onClick={() => void create()}
          aria-label="New conversation"
          title="New conversation"
        >
          <Plus size={16} />
          New
        </button>
        {onCollapse && (
          <button
            className="quiet"
            onClick={onCollapse}
            aria-label="Close design assistant"
            title="Collapse assistant"
          >
            <PanelRightClose size={20} />
          </button>
        )}
      </header>
      <div className="assistant-task-choice field">
        <label htmlFor="assistant-task">Next task</label>
        <select
          id="assistant-task"
          aria-label="Task for next conversation"
          value={task}
          disabled={busy}
          onChange={(event) => setTask(event.target.value as AssistantTask)}
        >
          {Object.entries(assistantTasks).map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
      </div>
      <div className="assistant-history-control">
        {current && (
          <p className="assistant-conversation-context">
            <strong>
              {current.task
                ? assistantTasks[current.task]
                : "Earlier conversation"}
            </strong>
            <span>
              AI via {current.agent}
              {recordedModel ? ` · ${recordedModel}` : ""}
            </span>
          </p>
        )}
        <details className="assistant-settings">
          <summary>History</summary>
          <div className="assistant-history-popover">
            <p>
              A new task starts a separate conversation. Your current dialogue
              stays unchanged.
            </p>
            {current && (
              <p className="feature-meta">
                {!current.task &&
                  "Task was not recorded for this earlier conversation. "}
                {current.journal?.verified
                  ? "Recorded history verified"
                  : "History verification not yet available"}
                {current.model_selection
                  ? ` · Profile ${current.model_selection.profile_id}, revision ${current.model_selection.profile_revision}`
                  : " · Model profile not recorded"}
              </p>
            )}
            <div className="feature-toolbar">
              {sessions.length > 0 && (
                <div className="field">
                  <label htmlFor="design-conversation">
                    Recorded conversation
                  </label>
                  <select
                    id="design-conversation"
                    value={id ?? ""}
                    disabled={busy}
                    onChange={(event) => {
                      setSelected(event.target.value);
                      setConversation(null);
                      setUncertain(null);
                    }}
                  >
                    {sessions.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.task
                          ? assistantTasks[item.task]
                          : "Recorded conversation"}{" "}
                        · {new Date(item.created_at).toLocaleString()}
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <button
                className="secondary"
                disabled={busy}
                onClick={() => setRevision((value) => value + 1)}
              >
                <RefreshCw size={18} />
                Refresh record
              </button>
              <button className="secondary" onClick={onConfigure}>
                <Settings2 size={16} /> Configure AI
              </button>
            </div>
          </div>
        </details>
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {uncertain && (
        <p className="integration-status">
          The last request outcome is uncertain. Refresh reads the retained
          receipt; no model request is retried automatically.
        </p>
      )}
      {!current && (
        <div className="assistant-empty">
          <p>
            Choose a task, then start a conversation. The model runs when you
            send a message.
          </p>
          <button
            className="primary"
            disabled={busy}
            onClick={() => void create()}
          >
            <Plus size={18} />
            Start conversation
          </button>
        </div>
      )}
      {current && (
        <>
          <section
            className="assistant-turns"
            aria-label="Recorded design dialogue"
          >
            {current.turns?.length ? (
              current.turns.map((turn) => (
                <article
                  className="assistant-turn"
                  id={`assistant-turn-${turn.id}`}
                  key={turn.id}
                >
                  <header>
                    <strong>
                      {turn.reply ? "AI response" : "Product owner request"}
                    </strong>
                    <span className="feature-status">
                      {turn.status.replaceAll("_", " ")}
                    </span>
                  </header>
                  {turn.reply && (
                    <section>
                      <p
                        className="assistant-source-label"
                        title={`AI-generated · ${turn.provenance?.provider ?? current.agent} · ${turn.provenance?.model ?? "model not recorded"} · ${new Date(turn.provenance?.generated_at ?? turn.created_at).toLocaleString()}`}
                      >
                        AI via {turn.provenance?.provider ?? current.agent} ·{" "}
                        {new Date(
                          turn.provenance?.generated_at ?? turn.created_at,
                        ).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </p>
                      <RichMarkdown value={turn.reply.message} />
                      {turn.reply.questions.length > 0 && (
                        <>
                          <h4>Questions</h4>
                          <ol>
                            {turn.reply.questions.map((question, index) => (
                              <li key={index}>{question}</li>
                            ))}
                          </ol>
                        </>
                      )}
                      {turn.reply.assumptions.length > 0 && (
                        <details>
                          <summary>Assumptions to validate</summary>
                          <ul>
                            {turn.reply.assumptions.map((item, index) => (
                              <li key={index}>{item}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                      {turn.reply.citation_ids.length > 0 && (
                        <AssistantSources
                          ids={turn.reply.citation_ids}
                          context={turn.context}
                        />
                      )}
                      {turn.reply.proposal && (
                        <div className="assistant-proposal">
                          <h4>Proposed product definition</h4>
                          <p>
                            {turn.reply.proposal.customer} ·{" "}
                            {turn.reply.proposal.journey.length}{" "}
                            {turn.reply.proposal.journey.length === 1
                              ? "journey"
                              : "journeys"}{" "}
                            · {turn.reply.proposal.features?.length ?? 0}{" "}
                            {(turn.reply.proposal.features?.length ?? 0) === 1
                              ? "feature"
                              : "features"}
                          </p>
                          <button
                            className="primary"
                            disabled={
                              !current.journal?.verified ||
                              turn.status !== "completed" ||
                              !turn.provenance?.proposal_digest
                            }
                            onClick={() => {
                              if (!turn.provenance?.proposal_digest) return;
                              onUse(
                                {
                                  ...structuredClone(turn.reply!.proposal!),
                                  functional_reviewed: false,
                                  architecture: turn.reply!.proposal!
                                    .architecture
                                    ? {
                                        ...turn.reply!.proposal!.architecture,
                                        reviewed: false,
                                      }
                                    : null,
                                  design_review: null,
                                },
                                turn.provenance,
                              );
                            }}
                          >
                            Use proposal as wizard draft
                          </button>
                          <small>
                            This fills the reviewable draft. It does not save a
                            definition or approve the proposal.
                          </small>
                        </div>
                      )}
                    </section>
                  )}
                  <details className="assistant-owner-message">
                    <summary>Product owner message</summary>
                    <RichMarkdown value={turn.message} />
                  </details>
                  {turn.context?.attachments?.length ? (
                    <ConversationFileTray files={turn.context.attachments} />
                  ) : null}
                  {turn.status === "processing" &&
                    (turn.recovery?.eligible ||
                      turn.recovery?.legacy_confirmation_required) && (
                      <button
                        type="button"
                        className="secondary"
                        onClick={() => setRecoveryTurn(turn)}
                      >
                        Review interrupted response
                      </button>
                    )}
                  {turn.detail && <p className="feature-meta">{turn.detail}</p>}
                  {turn.failure_kind && (
                    <p>
                      Failure category: {turn.failure_kind.replaceAll("_", " ")}
                    </p>
                  )}
                </article>
              ))
            ) : (
              <p>
                No turns have been recorded. Ask a concrete question to begin.
              </p>
            )}
          </section>
          <form
            onSubmit={(event) => void send(event)}
            className="assistant-composer"
          >
            <div className="field">
              <label htmlFor="assistant-message">Product design message</label>
              <textarea
                id="assistant-message"
                required
                maxLength={12000}
                rows={3}
                value={message}
                disabled={busy || active || Boolean(uncertain)}
                onChange={(event) => {
                  setMessage(event.target.value);
                  setContextNote(false);
                }}
                placeholder="Describe the customer and problem, or answer the recorded design questions."
              />
            </div>
            {contextNote && (
              <p className="assistant-file-guidance">
                Current editor context · inspect and edit before sending.
              </p>
            )}
            <ConversationFileTray
              files={selectedFiles}
              onRemove={(id) =>
                selectFiles(
                  current.id,
                  selectedFiles.filter((file) => file.id !== id),
                )
              }
            />
            {selectedFiles.some((file) => file.kind === "image") && (
              <p className="assistant-file-guidance">
                {imageCapability?.conversation === current.id &&
                imageCapability.status === "verified"
                  ? "Image input is verified for this conversation’s recorded model."
                  : imageCapability?.conversation !== current.id
                    ? "Checking image support for this conversation’s recorded model…"
                    : "Image input is not verified for this conversation. The server will reject unsupported image requests before calling the model."}
              </p>
            )}
            {fileError && (
              <p className="alert" role="alert">
                {fileError}
              </p>
            )}
            <div className="assistant-composer-actions">
              <button
                type="button"
                className="secondary"
                disabled={busy || active || Boolean(uncertain)}
                onClick={() => setFilesOpen(true)}
              >
                <Paperclip size={18} /> Files
                {selectedFiles.length ? ` (${selectedFiles.length})` : ""}
              </button>
              <button
                className="primary"
                aria-label={
                  busy || active
                    ? "Waiting for recorded response"
                    : "Send design message"
                }
                disabled={
                  busy ||
                  active ||
                  Boolean(uncertain) ||
                  !message.trim() ||
                  Boolean(fileError)
                }
              >
                <Send size={18} />
                {busy || active ? "Waiting…" : "Send message"}
              </button>
            </div>
            <p className="feature-meta">
              Recorded per product · no automatic apply or retry.
            </p>
          </form>
        </>
      )}
      {recoveryTurn && current && (
        <AssistantRecovery
          key={`${current.id}:${recoveryTurn.id}`}
          connection={connection}
          repository={repository}
          conversation={current.id}
          turn={recoveryTurn}
          onClose={() => setRecoveryTurn(null)}
          onRecorded={() => setRevision((value) => value + 1)}
        />
      )}
      {contextReview && (
        <StepContextReview
          message={message}
          context={contextReview}
          onClose={() => setContextReview(null)}
          onUse={(value) => {
            setMessage(value);
            setContextNote(true);
            setContextReview(null);
          }}
        />
      )}
      {filesOpen && current && (
        <ConversationFiles
          key={current.id}
          connection={connection}
          repository={repository}
          conversation={current.id}
          selected={selectedFiles}
          onChange={(files) => selectFiles(current.id, files)}
          onClose={() => setFilesOpen(false)}
        />
      )}
    </section>
  );
}
