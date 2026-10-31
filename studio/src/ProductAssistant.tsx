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
  History,
  UserRound,
  ArrowLeft,
} from "lucide-react";
import "./ProductAssistant.css";
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
    [uncertainRequests, setUncertainRequests] = useState<
      Record<string, string>
    >({}),
    [creationUncertain, setCreationUncertain] = useState(false),
    [listReady, setListReady] = useState(false),
    [historyOpen, setHistoryOpen] = useState(false),
    [newDraft, setNewDraft] = useState(false);
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
  const operation = useRef(false),
    editor = useRef<HTMLTextAreaElement>(null);
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
        setListReady(true);
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
  const id =
    creationUncertain || newDraft
      ? undefined
      : sessions.some((item) => item.id === selected)
        ? selected
        : sessions[0]?.id;
  const recordedUncertain = id ? uncertainRequests[id] : undefined;
  const uncertain = newDraft ? null : recordedUncertain;
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
            recordedUncertain &&
            value.turns?.some((turn) => turn.request_id === recordedUncertain)
          )
            setUncertainRequests((items) => {
              if (items[id] !== recordedUncertain) return items;
              const next = { ...items };
              delete next[id];
              return next;
            });
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
  }, [connection, repository, id, revision, recordedUncertain]);
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
    if (sourceTarget) {
      setSelected(sourceTarget.conversation_id);
      setNewDraft(false);
      setHistoryOpen(false);
    }
  }, [sourceTarget]);
  const current = !newDraft && conversation?.id === id ? conversation : null;
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
  const locked =
    busy ||
    active ||
    Boolean(uncertain) ||
    creationUncertain ||
    !listReady ||
    Boolean(id && !current && !newDraft);
  useEffect(() => {
    const resize = () => {
      if (!editor.current) return;
      editor.current.style.height = "auto";
      editor.current.style.height = `${Math.min(editor.current.scrollHeight, 180)}px`;
    };
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [message, historyOpen]);
  function begin() {
    if (operation.current) return null;
    operation.current = true;
    const controller = new AbortController();
    pending.current?.abort();
    pending.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }
  function finish(controller: AbortController) {
    if (pending.current !== controller) return;
    operation.current = false;
    if (!controller.signal.aborted) setBusy(false);
  }
  async function createSession(controller: AbortController) {
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
      if (controller.signal.aborted)
        throw new DOMException("Request aborted", "AbortError");
      if (
        !/^[a-f0-9]{32}$/.test(value.id) ||
        value.repository_id !== repository ||
        (value.project_id ?? null) !== (projectId ?? null)
      )
        throw new Error("The created conversation could not be confirmed.");
      setSessions((items) => [
        value,
        ...items.filter((item) => item.id !== value.id),
      ]);
      setSelected(value.id);
      setConversation(value);
      setNewDraft(false);
      setCreationUncertain(false);
      setRevision((value) => value + 1);
      return value;
    } catch (cause) {
      if (
        !controller.signal.aborted &&
        !(
          cause instanceof ApiError &&
          [400, 401, 403, 404, 409, 422].includes(cause.status)
        )
      )
        setCreationUncertain(true);
      throw cause;
    }
  }
  async function attach() {
    if (locked) return;
    if (current) {
      setFilesOpen(true);
      return;
    }
    const controller = begin();
    if (!controller) return;
    try {
      await createSession(controller);
      if (!controller.signal.aborted) setFilesOpen(true);
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      finish(controller);
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (locked || !message.trim() || fileError) return;
    const controller = begin();
    if (!controller) return;
    let requestId = "";
    let targetId = current?.id ?? "";
    try {
      const target = current ?? (await createSession(controller));
      targetId = target.id;
      if (controller.signal.aborted) return;
      requestId = crypto.randomUUID().replaceAll("-", "");
      await request<Turn>(
        connection,
        `/repositories/${repository}/assistant/conversations/${target.id}/turns`,
        {
          method: "POST",
          body: JSON.stringify({
            request_id: requestId,
            message: message.trim(),
            ...(current?.id === target.id && selectedFiles.length
              ? { attachment_ids: selectedFiles.map((file) => file.id) }
              : {}),
          }),
          signal: controller.signal,
        },
      );
      if (controller.signal.aborted) return;
      setMessage("");
      setContextNote(false);
      selectFiles(target.id, []);
      setRevision((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) {
        const rejected =
          cause instanceof ApiError && [409, 422].includes(cause.status);
        if (!rejected && requestId)
          setUncertainRequests((items) => ({
            ...items,
            [targetId]: requestId,
          }));
        setError(
          rejected || !requestId
            ? failure(cause)
            : `${failure(cause)} Refresh the recorded conversation before requesting another turn.`,
        );
      }
    } finally {
      finish(controller);
    }
  }
  return (
    <section className="product-assistant" data-empty={!current?.turns?.length}>
      <header className="assistant-toolbar">
        <h2>AI design assistant</h2>
        <div className="assistant-toolbar-actions">
          <button
            type="button"
            className="assistant-icon"
            aria-label="New conversation"
            title="New conversation"
            disabled={busy || creationUncertain || !listReady}
            onClick={() => {
              setNewDraft(true);
              setHistoryOpen(false);
              editor.current?.focus();
            }}
          >
            <Plus size={18} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="assistant-icon"
            aria-label="Conversation history"
            title="Conversation history"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((value) => !value)}
          >
            <History size={18} aria-hidden="true" />
          </button>
          {onCollapse && (
            <button
              type="button"
              className="assistant-icon"
              onClick={onCollapse}
              aria-label="Close design assistant"
              title="Collapse assistant"
            >
              <PanelRightClose size={18} aria-hidden="true" />
            </button>
          )}
        </div>
      </header>
      {!current && !historyOpen && (
        <div className="assistant-task-choice">
          <label htmlFor="assistant-task">New conversation</label>
          <select
            id="assistant-task"
            aria-label="Task for next conversation"
            value={task}
            disabled={busy || creationUncertain}
            onChange={(event) => setTask(event.target.value as AssistantTask)}
          >
            {Object.entries(assistantTasks).map(([id, name]) => (
              <option key={id} value={id}>
                {name}
              </option>
            ))}
          </select>
        </div>
      )}
      {current && !historyOpen && (
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
      {historyOpen && (
        <section
          className="assistant-history"
          aria-label="Recorded conversations"
        >
          <button
            type="button"
            className="assistant-history-back"
            onClick={() => setHistoryOpen(false)}
          >
            <ArrowLeft size={16} aria-hidden="true" />
            Back to conversation
          </button>
          {sessions.length ? (
            <div className="field">
              <label htmlFor="design-conversation">Recorded conversation</label>
              <select
                id="design-conversation"
                value={creationUncertain ? "" : (id ?? "")}
                disabled={busy}
                onChange={(event) => {
                  setSelected(event.target.value);
                  setConversation(null);
                  setNewDraft(false);
                  setCreationUncertain(false);
                  setHistoryOpen(false);
                  setRevision((value) => value + 1);
                }}
              >
                {!id && (
                  <option value="" disabled>
                    Choose a recorded conversation
                  </option>
                )}
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
          ) : (
            <p>No conversations have been recorded.</p>
          )}
          {current && (
            <p className="assistant-history-proof">
              {!current.task && "Task was not recorded. "}
              {current.journal?.verified
                ? "Recorded history verified"
                : "History verification not yet available"}
              {current.model_selection
                ? ` · Profile ${current.model_selection.profile_id}, revision ${current.model_selection.profile_revision}`
                : " · Model profile not recorded"}
            </p>
          )}
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => setRevision((value) => value + 1)}
          >
            <RefreshCw size={16} aria-hidden="true" />
            Refresh record
          </button>
          <button type="button" className="secondary" onClick={onConfigure}>
            <Settings2 size={16} aria-hidden="true" />
            Configure AI
          </button>
        </section>
      )}
      {creationUncertain && (
        <p className="integration-status" role="status">
          The new conversation could not be confirmed. Refresh history and
          choose its recorded conversation before sending.
        </p>
      )}
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
      {!current && !historyOpen && (
        <p className="assistant-empty">
          Describe the customer and problem, or choose a task to work on.
        </p>
      )}
      {current && (
        <>
          <section
            className="assistant-turns"
            hidden={historyOpen}
            role="log"
            aria-live="polite"
            aria-relevant="additions"
            aria-label="Recorded design dialogue"
          >
            {current.turns?.length ? (
              current.turns.map((turn) => (
                <div
                  className="assistant-turn"
                  id={`assistant-turn-${turn.id}`}
                  key={turn.id}
                >
                  <article className="assistant-message assistant-user-message">
                    <header>
                      <span
                        className="assistant-message-mark"
                        aria-hidden="true"
                      >
                        <UserRound size={15} />
                      </span>
                      <strong>You</strong>
                      <time dateTime={turn.created_at}>
                        {new Date(turn.created_at).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </time>
                    </header>
                    <RichMarkdown value={turn.message} />
                    {turn.context?.attachments?.length ? (
                      <ConversationFileTray files={turn.context.attachments} />
                    ) : null}
                  </article>
                  <article className="assistant-message assistant-ai-message">
                    <header>
                      <span
                        className="assistant-message-mark"
                        aria-hidden="true"
                      >
                        <MessageSquare size={15} />
                      </span>
                      <strong>AI response</strong>
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
                              This fills the reviewable draft. It does not save
                              a definition or approve the proposal.
                            </small>
                          </div>
                        )}
                      </section>
                    )}
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
                    {turn.detail && (
                      <p className="feature-meta">{turn.detail}</p>
                    )}
                    {turn.failure_kind && (
                      <p>
                        Failure category:{" "}
                        {turn.failure_kind.replaceAll("_", " ")}
                      </p>
                    )}
                  </article>
                </div>
              ))
            ) : (
              <p>
                No turns have been recorded. Ask a concrete question to begin.
              </p>
            )}
          </section>
        </>
      )}
      <form
        onSubmit={(event) => void send(event)}
        className="assistant-composer"
        aria-label="Design message"
      >
        <fieldset disabled={locked} className="assistant-composer-fields">
          <ConversationFileTray
            files={selectedFiles}
            onRemove={(id) =>
              current &&
              selectFiles(
                current.id,
                selectedFiles.filter((file) => file.id !== id),
              )
            }
          />
          <div className="assistant-editor-row">
            <button
              type="button"
              className="assistant-icon assistant-attach"
              aria-label="Files"
              title="Attach files"
              onClick={() => void attach()}
            >
              <Paperclip size={18} aria-hidden="true" />
            </button>
            <label className="assistant-sr-only" htmlFor="assistant-message">
              Product design message
            </label>
            <textarea
              ref={editor}
              id="assistant-message"
              required
              maxLength={12000}
              rows={1}
              value={message}
              onChange={(event) => {
                setMessage(event.target.value);
                setContextNote(false);
              }}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing &&
                  event.nativeEvent.keyCode !== 229
                ) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Write a message…"
            />
            <button
              className="assistant-icon assistant-send"
              aria-label={
                busy || active
                  ? "Waiting for recorded response"
                  : "Send design message"
              }
              title={
                busy || active
                  ? "Waiting for recorded response"
                  : "Send message"
              }
              disabled={locked || !message.trim() || Boolean(fileError)}
            >
              <Send size={18} aria-hidden="true" />
            </button>
          </div>
          {contextNote && (
            <p className="assistant-file-guidance">
              Current editor context · inspect and edit before sending.
            </p>
          )}
          {selectedFiles.some((file) => file.kind === "image") && (
            <p className="assistant-file-guidance">
              {imageCapability?.conversation === current?.id &&
              imageCapability?.status === "verified"
                ? "Image input is verified for this conversation’s recorded model."
                : imageCapability?.conversation !== current?.id
                  ? "Checking image support for this conversation’s recorded model…"
                  : "Image input is not verified for this conversation. The server will reject unsupported image requests before calling the model."}
            </p>
          )}
          {fileError && (
            <p className="alert" role="alert">
              {fileError}
            </p>
          )}
        </fieldset>
        <p
          className="assistant-composer-note"
          role={busy || active ? "status" : undefined}
        >
          {busy || active
            ? "Waiting for the recorded response…"
            : "Enter to send · Shift+Enter for a new line"}
        </p>
      </form>
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
