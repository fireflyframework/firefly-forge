/* Copyright 2026 Firefly Software Foundation. SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useState } from "react";
import {
  Check,
  FileText,
  Image,
  Paperclip,
  RefreshCw,
  Upload,
  Download,
  X,
} from "lucide-react";
import { ApiError, normalizeBase, request, type Connection } from "./api";
import "./ConversationFiles.css";

export type ConversationAttachment = {
  id: string;
  request_id: string;
  filename: string;
  mime_type: string;
  kind: "document" | "image";
  content_bytes: number;
  sha256: string;
  status: "ready";
  text_chars: number;
  truncated: boolean;
  created_at: string;
};
const extensions = [
  "txt",
  "md",
  "json",
  "yaml",
  "yml",
  "csv",
  "pdf",
  "docx",
  "png",
  "jpg",
  "jpeg",
  "webp",
];
const issue = (cause: unknown) =>
  cause instanceof Error
    ? cause.message
    : "The file request could not be completed.";
export function attachmentSelectionError(
  files: ConversationAttachment[],
): string {
  if (files.length > 8)
    return "Choose no more than eight files for one message.";
  if (files.filter((file) => file.kind === "image").length > 5)
    return "Choose no more than five images for one message.";
  if (files.reduce((count, file) => count + file.text_chars, 0) > 60000)
    return "Choose fewer documents: one message accepts 60,000 extracted text characters.";
  return "";
}
export function ConversationFileTray({
  files,
  onRemove,
}: {
  files: ConversationAttachment[];
  onRemove?: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  if (!files.length) return null;
  const visible = expanded ? files : files.slice(0, 1);
  return (
    <div
      className="conversation-file-tray"
      aria-label={
        onRemove ? "Files for next message" : "Recorded message files"
      }
    >
      {visible.map((file) => (
        <article className="conversation-file-chip" key={file.id}>
          {file.kind === "image" ? <Image size={16} /> : <FileText size={16} />}
          <span>
            <strong title={file.filename}>{file.filename}</strong>
            <small>
              {file.kind === "image" ? "Image" : "Document"} ·{" "}
              {file.truncated ? "Text excerpt" : "Ready"}
            </small>
          </span>
          {onRemove && (
            <button
              type="button"
              className="quiet"
              aria-label={`Remove ${file.filename} from message`}
              onClick={() => onRemove(file.id)}
            >
              <X size={16} />
            </button>
          )}
        </article>
      ))}
      {files.length > 1 && (
        <button
          type="button"
          className="quiet conversation-more-files"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded
            ? "Show fewer"
            : `+${files.length - 1} ${files.length === 2 ? "file" : "files"}`}
        </button>
      )}
    </div>
  );
}
export function ConversationFiles({
  connection,
  repository,
  conversation,
  selected,
  onChange,
  onClose,
}: {
  connection: Connection;
  repository: string;
  conversation: string;
  selected: ConversationAttachment[];
  onChange: (files: ConversationAttachment[]) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    input = useRef<HTMLInputElement>(null),
    alive = useRef(true),
    generation = useRef(0);
  const abort = useRef<AbortController | null>(null),
    selection = useRef(selected);
  selection.current = selected;
  const [files, setFiles] = useState<ConversationAttachment[]>([]),
    [loading, setLoading] = useState(true),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [pending, setPending] = useState<{
    request_id: string;
    filename: string;
    content_base64: string;
    bytes: number;
  } | null>(null);
  const [reviewed, setReviewed] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [dragging, setDragging] = useState(false);
  const path = `/repositories/${repository}/assistant/conversations/${conversation}/attachments`;
  useEffect(() => {
    alive.current = true;
    generation.current += 1;
    const node = dialog.current;
    node?.showModal();
    const controller = new AbortController();
    abort.current = controller;
    request<ConversationAttachment[]>(connection, path, {
      signal: controller.signal,
    })
      .then((items) => {
        if (!controller.signal.aborted) setFiles(items);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(issue(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      alive.current = false;
      generation.current += 1;
      controller.abort();
      abort.current?.abort();
      node?.close();
    };
  }, [connection, path]);
  function choose(files: ConversationAttachment[]) {
    const invalid = attachmentSelectionError(files);
    if (invalid) {
      setError(invalid);
      return;
    }
    onChange(files);
    setError("");
  }
  async function download(file: ConversationAttachment) {
    const controller = new AbortController(),
      currentGeneration = generation.current;
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const response = await fetch(
        `${normalizeBase(connection.base)}${path}/${encodeURIComponent(file.id)}/content`,
        {
          credentials: "include",
          signal: controller.signal,
          headers: connection.token
            ? { Authorization: `Bearer ${connection.token}` }
            : {},
        },
      );
      if (!response.ok)
        throw new Error(`Original file download failed (${response.status}).`);
      const blob = await response.blob();
      if (blob.size !== file.content_bytes)
        throw new Error(
          "The downloaded file does not match its recorded size. Refresh the recorded files before trying again.",
        );
      if (!alive.current || generation.current !== currentGeneration) return;
      const url = URL.createObjectURL(blob),
        link = document.createElement("a");
      link.href = url;
      link.download = file.filename;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (cause) {
      if (
        alive.current &&
        generation.current === currentGeneration &&
        !controller.signal.aborted
      )
        setError(issue(cause));
    } finally {
      if (alive.current && generation.current === currentGeneration)
        setBusy(false);
    }
  }
  async function inspect(file: File) {
    if (busy || uncertain || loading || files.length >= 20) return;
    const currentGeneration = generation.current;
    setError("");
    setPending(null);
    setReviewed(false);
    if (
      !extensions.includes(file.name.split(".").at(-1)?.toLowerCase() ?? "")
    ) {
      setError("Choose a supported document or PNG, JPEG, or WebP image.");
      return;
    }
    if (!file.size || file.size > 1000000) {
      setError("Choose a nonempty file no larger than 1 MB (1,000,000 bytes).");
      return;
    }
    setBusy(true);
    try {
      const bytes = new Uint8Array(await file.arrayBuffer());
      let binary = "";
      for (let offset = 0; offset < bytes.length; offset += 32768)
        binary += String.fromCharCode(
          ...bytes.subarray(offset, offset + 32768),
        );
      if (alive.current && generation.current === currentGeneration)
        setPending({
          request_id: crypto.randomUUID().replaceAll("-", ""),
          filename: file.name,
          content_base64: btoa(binary),
          bytes: bytes.length,
        });
    } catch (cause) {
      if (alive.current && generation.current === currentGeneration)
        setError(issue(cause));
    } finally {
      if (alive.current && generation.current === currentGeneration)
        setBusy(false);
    }
  }
  function attach(file: ConversationAttachment) {
    const next = selection.current.some((item) => item.id === file.id)
      ? selection.current
      : [...selection.current, file];
    choose(next);
  }
  async function upload() {
    if (!pending || !reviewed || busy) return;
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const value = await request<ConversationAttachment>(connection, path, {
        method: "POST",
        body: JSON.stringify({
          request_id: pending.request_id,
          filename: pending.filename,
          content_base64: pending.content_base64,
        }),
        signal: controller.signal,
      });
      if (!alive.current || controller.signal.aborted) return;
      if (value.status !== "ready" || !value.id)
        throw new Error(
          "The saved file receipt was not returned. Check recorded files before uploading again.",
        );
      setFiles((items) => [
        ...items.filter((item) => item.id !== value.id),
        value,
      ]);
      attach(value);
      setPending(null);
      setUncertain(false);
      setReviewed(false);
    } catch (cause) {
      if (!alive.current || controller.signal.aborted) return;
      const rejected =
        cause instanceof ApiError && [409, 422].includes(cause.status);
      setUncertain(!rejected);
      setError(
        rejected
          ? issue(cause)
          : `${issue(cause)} Check recorded files before retrying this same upload.`,
      );
    } finally {
      if (alive.current && !controller.signal.aborted) setBusy(false);
    }
  }
  async function refresh() {
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError("");
    try {
      const items = await request<ConversationAttachment[]>(connection, path, {
        signal: controller.signal,
      });
      if (!alive.current || controller.signal.aborted) return;
      setFiles(items);
      const found =
        pending && items.find((file) => file.request_id === pending.request_id);
      if (found) {
        attach(found);
        setPending(null);
        setUncertain(false);
        setReviewed(false);
      } else if (uncertain)
        setError(
          "No matching file receipt was returned. You can retry the same upload; its request ID and content are retained.",
        );
    } catch (cause) {
      if (alive.current && !controller.signal.aborted) setError(issue(cause));
    } finally {
      if (alive.current && !controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="conversation-files-dialog"
      aria-label="Conversation files"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
    >
      <header>
        <div>
          <h2>
            <Paperclip size={24} /> Files for this conversation
          </h2>
          <p>
            Upload a file, then choose what to include in your next message.
          </p>
        </div>
        <button
          type="button"
          className="quiet"
          disabled={busy}
          aria-label="Close conversation files"
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      <div className="conversation-files-body">
        <div
          className={`conversation-file-drop ${dragging ? "dragging" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            const file = event.dataTransfer.files[0];
            if (file) void inspect(file);
          }}
        >
          <input
            ref={input}
            type="file"
            className="sr-only"
            aria-label="Choose conversation file"
            accept={extensions.map((extension) => `.${extension}`).join(",")}
            disabled={busy || uncertain || loading || files.length >= 20}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (file) void inspect(file);
            }}
          />
          <button
            type="button"
            className="secondary"
            disabled={busy || uncertain || loading || files.length >= 20}
            onClick={() => input.current?.click()}
          >
            <Upload size={18} /> Choose a file
          </button>
          <p>or drop a file here · up to 1 MB</p>
          <small>
            Text, PDF, DOCX, PNG, JPEG, or WebP. Scanned or encrypted PDFs
            cannot be extracted.
          </small>
        </div>
        {pending && (
          <section className="conversation-file-pending">
            <h3>{pending.filename}</h3>
            <p>
              {pending.bytes.toLocaleString()} bytes ·{" "}
              {uncertain
                ? "Upload outcome not yet confirmed"
                : "Ready to upload"}
            </p>
            <label className="check-choice">
              <input
                type="checkbox"
                checked={reviewed}
                disabled={busy || uncertain}
                onChange={(event) => setReviewed(event.target.checked)}
              />{" "}
              I checked this file for credentials or secrets.
            </label>
            <div className="conversation-file-actions">
              {uncertain ? (
                <>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => void refresh()}
                  >
                    <RefreshCw size={16} /> Check upload
                  </button>
                  <button
                    type="button"
                    className="secondary"
                    disabled={busy}
                    onClick={() => void upload()}
                  >
                    Retry same upload
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="primary"
                    disabled={busy || !reviewed}
                    onClick={() => void upload()}
                  >
                    <Upload size={16} />{" "}
                    {busy ? "Saving…" : "Upload and attach"}
                  </button>
                  <button
                    type="button"
                    className="quiet"
                    disabled={busy}
                    onClick={() => {
                      setPending(null);
                      setReviewed(false);
                    }}
                  >
                    Choose another file
                  </button>
                </>
              )}
            </div>
          </section>
        )}
        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}
        <section className="conversation-saved-files">
          <h3>Saved files</h3>
          {loading ? (
            <p role="status">Loading recorded files…</p>
          ) : !files.length && !error ? (
            <p>No files have been saved to this conversation.</p>
          ) : (
            files.map((file) => (
              <article key={file.id}>
                <label className="check-choice">
                  <input
                    type="checkbox"
                    aria-label={`Include ${file.filename} in next message`}
                    checked={selected.some((item) => item.id === file.id)}
                    disabled={busy || file.status !== "ready"}
                    onChange={(event) =>
                      choose(
                        event.target.checked
                          ? [...selected, file]
                          : selected.filter((item) => item.id !== file.id),
                      )
                    }
                  />
                  <span>
                    <strong>{file.filename}</strong>
                    <small>
                      {file.kind === "image" ? "Image" : "Document"} ·{" "}
                      {(file.content_bytes / 1000).toLocaleString()} KB · Ready
                    </small>
                  </span>
                </label>
                <button
                  type="button"
                  className="secondary conversation-file-download"
                  aria-label={`Download ${file.filename}`}
                  disabled={busy || file.status !== "ready"}
                  onClick={() => void download(file)}
                >
                  <Download size={16} />
                  Download original
                </button>
                {file.truncated && (
                  <p>
                    Extracted text was shortened to{" "}
                    {file.text_chars.toLocaleString()} characters.
                  </p>
                )}
                <details>
                  <summary>File details</summary>
                  <p>
                    {file.mime_type} ·{" "}
                    {new Date(file.created_at).toLocaleString()}
                  </p>
                  <code>{file.sha256}</code>
                </details>
              </article>
            ))
          )}
        </section>
        <p className="conversation-files-note">
          Files are saved with this conversation. Removing a selection detaches
          it from the next message.
        </p>
      </div>
      <footer>
        <span>{selected.length} of 8 files selected</span>
        <button
          type="button"
          className="primary"
          disabled={busy}
          onClick={onClose}
        >
          <Check size={18} /> Done
        </button>
      </footer>
    </dialog>
  );
}
